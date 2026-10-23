import { accountFetch as fetch } from "../lib/accountFetch";
import { useCallback, useEffect, useId, useRef, useState, type SetStateAction } from "react";
import { useSessionStore, type AttachmentMetadata } from "../stores/sessionStore";

export const MAX_ATTACHMENTS_PER_MESSAGE = 10;
const MAX_FILE_BYTES = 25 * 1024 * 1024;

export interface PendingAttachment {
  uid: string;
  file: File;
  ownerSessionId: string | null;
  generation: number;
  status: "uploading" | "ready" | "error";
  meta?: AttachmentMetadata;
  error?: string;
}

interface RejectedFile { name: string; reason: string }

function uploadState(file: File, ownerSessionId: string | null, generation: number) {
  const error = file.size === 0 ? "The file is empty" : file.size > MAX_FILE_BYTES ? "The file exceeds 25 MB" : undefined;
  return { ownerSessionId, generation, status: error ? "error" as const : "uploading" as const, meta: undefined, error };
}

/** A File is the draft; an upload ID belongs to one session and attempt. */
export function useDraftAttachments(activeSessionId: string | null) {
  // These handles identify Files only within this draft, never wire requests.
  const scope = useId();
  const sequence = useRef(0);
  const [draft, setDraft] = useState<{ owner: string | null; attachments: PendingAttachment[]; rejected: RejectedFile[] }>(
    { owner: activeSessionId, attachments: [], rejected: [] });
  if (draft.owner !== activeSessionId) {
    // Adjust derived state before React commits the new session. Uploads
    // belong in the effect; the synchronous owner transition does not.
    setDraft({ owner: activeSessionId, rejected: draft.rejected, attachments: draft.attachments.map((file) => ({
      ...file, ...uploadState(file.file, activeSessionId, file.generation + 1),
    })) });
  }
  const attachments = draft.attachments;
  const setAttachments = useCallback((update: SetStateAction<PendingAttachment[]>) => {
    setDraft((current) => ({ ...current, attachments: typeof update === "function" ? update(current.attachments) : update }));
  }, []);
  const requests = useRef(new Map<string, { owner: string; generation: number; controller: AbortController }>());

  useEffect(() => {
    for (const [uid, request] of requests.current) {
      if (!attachments.some((file) => file.uid === uid && file.generation === request.generation &&
          file.ownerSessionId === request.owner) || request.owner !== activeSessionId) {
        request.controller.abort();
        requests.current.delete(uid);
      }
    }
    if (!activeSessionId) return;
    for (const file of attachments) {
      if (file.ownerSessionId !== activeSessionId || file.status !== "uploading" || requests.current.has(file.uid)) continue;
      const owner = activeSessionId;
      const { generation } = file;
      const controller = new AbortController();
      const captured = { owner, generation, controller };
      requests.current.set(file.uid, captured);
      const form = new FormData();
      form.append("file", file.file, file.file.name);
      const update = (state: Partial<PendingAttachment>) => setAttachments((current) => current.map((item) =>
        item.uid === file.uid && item.ownerSessionId === owner && item.generation === generation
          ? { ...item, ...state } : item));
      void fetch(`${window.location.origin}/api/sessions/${encodeURIComponent(owner)}/attachments`, {
        method: "POST", headers: { Authorization: `Bearer ${useSessionStore.getState().token}` },
        body: form, signal: controller.signal,
      }).then(async (response) => {
        if (!response.ok) throw new Error((await response.text()) || `upload failed (${response.status})`);
        const meta = await response.json() as AttachmentMetadata;
        if (!controller.signal.aborted) update({ status: "ready", meta });
      }).catch((error: unknown) => {
        if (!controller.signal.aborted) update({ status: "error", error: error instanceof Error ? error.message : "Upload failed" });
      }).finally(() => {
        if (requests.current.get(file.uid) === captured) requests.current.delete(file.uid);
      });
    }
  }, [activeSessionId, attachments, setAttachments]);

  useEffect(() => {
    const pending = requests.current;
    return () => {
      for (const request of pending.values()) request.controller.abort();
      pending.clear();
    };
  }, []);

  const addFiles = useCallback((files: File[]) => {
    const incoming = files.map((file) => ({ uid: `${scope}-${++sequence.current}`, file }));
    setDraft((current) => {
      if (current.owner === null) return current;
      const remaining = Math.max(0, MAX_ATTACHMENTS_PER_MESSAGE - current.attachments.length);
      return { ...current, rejected: [...current.rejected, ...files.slice(remaining).map((file) => ({
        name: file.name, reason: `At most ${MAX_ATTACHMENTS_PER_MESSAGE} attachments per message`,
      }))], attachments: [
        ...current.attachments, ...incoming.slice(0, remaining).map(({ uid, file }): PendingAttachment => ({
          uid, file, ...uploadState(file, current.owner, 1),
        })),
      ] };
    });
  }, [scope]);
  const removeAttachment = useCallback((uid: string) => {
    setAttachments((current) => current.filter((file) => file.uid !== uid));
  }, [setAttachments]);
  const retryAttachment = useCallback((uid: string) => {
    setAttachments((current) => current.map((file) => file.uid !== uid ? file : {
      ...file, ...uploadState(file.file, file.ownerSessionId, file.generation + 1),
    }));
  }, [setAttachments]);
  const restoreAttachments = useCallback((files: PendingAttachment[]) => {
    // Rejected input may refer to deleted IDs even in its original session.
    setAttachments(files.map((file) => ({
      ...file, ...uploadState(file.file, activeSessionId, file.generation + 1),
    })));
  }, [activeSessionId, setAttachments]);
  const dismissRejectedFiles = useCallback(() => setDraft((current) => ({ ...current, rejected: [] })), []);
  const clearAttachments = useCallback(() => setDraft((current) => ({ ...current, attachments: [], rejected: [] })), []);
  const unready = draft.rejected.length > 0 || attachments.some((file) => file.ownerSessionId !== activeSessionId || file.status !== "ready" || !file.meta);
  const readyIds = attachments.filter((file) => file.ownerSessionId === activeSessionId && file.status === "ready" && file.meta)
    .map((file) => file.meta!.id);
  return { attachments, rejectedFiles: draft.rejected, dismissRejectedFiles, addFiles, removeAttachment, retryAttachment,
    restoreAttachments, clearAttachments, unready, readyIds };
}
