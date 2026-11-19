import { useEffect, useRef, useState } from "react";
import type { ClientSubmissionStatus, ClientSubmissionSummary, SubmissionRecoveryPage } from "../api";
import { accountFetch as fetch } from "../lib/accountFetch";
import { chatDraft } from "../lib/chatDraft";
import { useSessionStore } from "../stores/sessionStore";
import { Button } from "./ui/button";

interface Props {
  sessionId: string;
  connected: boolean;
  disabled: boolean;
  onRestore: (saved: ClientSubmissionStatus, files: File[]) => void;
}

interface Dismissal {
  notice: ClientSubmissionSummary;
  error?: string;
}

interface PageRead {
  before: number;
  version: number;
}

interface RecoveryList {
  first: SubmissionRecoveryPage;
  additional: { read: PageRead; page: SubmissionRecoveryPage }[];
  pending: PageRead | null;
  error: string;
}

function nextCursor(list: RecoveryList) {
  return (list.additional.at(-1)?.page ?? list.first).next_cursor;
}

function replaceFirstPage(list: RecoveryList, first: SubmissionRecoveryPage, version: number): RecoveryList {
  // Keep pages read during this refresh whose cursors still connect to the first page.
  let cursor = first.next_cursor;
  const additional: RecoveryList["additional"] = [];
  for (const entry of list.additional) {
    if (entry.read.version !== version || entry.read.before !== cursor) continue;
    additional.push(entry);
    cursor = entry.page.next_cursor;
  }
  const pending = list.pending?.version === version && list.pending.before === cursor ? list.pending : null;
  return { first, additional, pending, error: "" };
}

async function readSavedInput<T>(url: string, token: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, { headers: { Authorization: `Bearer ${token}` }, signal });
  if (!response.ok) throw new Error(`Couldn't read saved input (${response.status})`);
  return await response.json() as T;
}

/** Reads saved input; only the composer can create a new execution. */
export function RecoverableInputs({ sessionId, connected, disabled, onRestore }: Props) {
  const token = useSessionStore((s) => s.token);
  const revision = useSessionStore((s) => s.dispatchStates[sessionId]?.revision);
  const generation = useSessionStore((s) => s.dispatchGeneration);
  const [list, setList] = useState<RecoveryList>({ first: { items: [] }, additional: [], pending: null, error: "" });
  const [dismissal, setDismissal] = useState<Dismissal | null>(null);
  const [details, setDetails] = useState<Record<string, ClientSubmissionStatus>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [missingFiles, setMissingFiles] = useState<Record<string, boolean>>({});
  const [refresh, setRefresh] = useState(0);
  const listVersion = useRef(0);
  const pendingRefresh = useRef<AbortController | null>(null);
  const requests = useRef(new Set<AbortController>());
  const latestRestore = useRef(onRestore);
  latestRestore.current = onRestore;
  const api = `${window.location.origin}/api/sessions/${encodeURIComponent(sessionId)}/submissions`;
  const pagination = list.pending;
  const cursor = nextCursor(list);

  useEffect(() => {
    const version = ++listVersion.current;
    setList((old) => old.pending ? { ...old, pending: null } : old);
    if (!token || !connected) return;
    const controller = new AbortController();
    pendingRefresh.current = controller;
    const release = () => {
      if (pendingRefresh.current === controller) pendingRefresh.current = null;
    };
    async function refreshPage() {
      try {
        const response = await fetch(api, { headers: { Authorization: `Bearer ${token}` }, signal: controller.signal });
        if (!response.ok) throw new Error(`Couldn't load saved inputs (${response.status})`);
        const value = await response.json() as SubmissionRecoveryPage;
        if (!value || !Array.isArray(value.items)) throw new Error("Saved input status was invalid. Try loading it again.");
        if (controller.signal.aborted || listVersion.current !== version) return;
        setList((old) => replaceFirstPage(old, value, version));
      } catch (err) {
        if (!controller.signal.aborted && listVersion.current === version) {
          const error = err instanceof Error ? err.message : "Couldn't load saved inputs";
          setList((old) => ({ ...old, error }));
        }
      } finally {
        release();
      }
    }
    void refreshPage();
    return () => { controller.abort(); release(); };
  }, [api, token, connected, revision, generation, refresh]);

  useEffect(() => {
    if (!pagination || !token) return;
    const controller = new AbortController();
    async function readMore(read: PageRead) {
      try {
        const page = await readSavedInput<SubmissionRecoveryPage>(`${api}?before=${read.before}`, token, controller.signal);
        if (controller.signal.aborted) return;
        setList((old) => old.pending === read
          ? { ...old, additional: [...old.additional, { read, page }], pending: null }
          : old);
      } catch (err) {
        if (controller.signal.aborted) return;
        const error = err instanceof Error ? err.message : "Couldn't load saved inputs";
        setList((old) => old.pending === read ? { ...old, error, pending: null } : old);
      }
    }
    void readMore(pagination);
    return () => controller.abort();
  }, [api, token, pagination]);

  useEffect(() => {
    const pending = requests.current;
    return () => { for (const request of pending) request.abort(); pending.clear(); };
  }, []);

  function clearItemFeedback(item?: ClientSubmissionSummary) {
    const retained = item && dismissal?.notice.client_request_id === item.client_request_id ? item : undefined;
    setList((old) => {
      // Keep the clicked notice available while the new action takes over.
      if (retained && ![old.first, ...old.additional.map(({ page }) => page)].some(
        (page) => page.items.some((row) => row.client_request_id === retained.client_request_id),
      )) {
        return { ...old, first: { ...old.first, items: [...old.first.items, retained] }, error: "" };
      }
      return { ...old, error: "" };
    });
    setDismissal(null);
  }

  async function action(item: ClientSubmissionSummary, operation: "review" | "restore" | "dismiss") {
    const dismissing = operation === "dismiss";
    if (busy || (pagination && !dismissing)) return;
    const controller = new AbortController();
    requests.current.add(controller);
    const restore = onRestore;
    const draft = operation === "restore" ? chatDraft() : null;
    const draftVersion = draft?.store.getState().version;
    setBusy(item.client_request_id);
    clearItemFeedback(dismissing ? undefined : item);
    if (dismissing) setDismissal({ notice: item });
    try {
      const url = `${api}/${encodeURIComponent(item.client_request_id)}`;
      if (dismissing) {
        const response = await fetch(`${url}/recovery`, { method: "DELETE", headers: { Authorization: `Bearer ${token}` }, signal: controller.signal });
        if (!response.ok) throw new Error(`Couldn't dismiss saved input (${response.status})`);
        if (!controller.signal.aborted) {
          // Reads already in flight may still describe the list before this write.
          ++listVersion.current;
          if (pendingRefresh.current) {
            pendingRefresh.current.abort();
            pendingRefresh.current = null;
            setRefresh((n) => n + 1);
          }
          const removeNotice = (page: SubmissionRecoveryPage) => ({
            ...page, items: page.items.filter((row) => row.client_request_id !== item.client_request_id),
          });
          setList((old) => ({
            ...old, first: removeNotice(old.first),
            additional: old.additional.map((entry) => ({ ...entry, page: removeNotice(entry.page) })),
            pending: null,
          }));
          setDismissal(null);
        }
        return;
      }
      const saved = await readSavedInput<ClientSubmissionStatus>(url, token, controller.signal);
      if (controller.signal.aborted) return;
      setDetails((old) => ({ ...old, [item.client_request_id]: saved }));
      if (operation === "review") return;
      if (!saved.execution.recoverable_input || !saved.input || saved.input.kind !== "message") throw new Error("This input cannot be restored. Review its current execution status.");
      const files: File[] = [];
      for (const meta of saved.input.attachments) {
        const response = await fetch(`${url}/attachments/${encodeURIComponent(meta.id)}`, {
          headers: { Authorization: `Bearer ${token}` }, signal: controller.signal,
        });
        if (!response.ok) {
          setMissingFiles((old) => ({ ...old, [item.client_request_id]: true }));
          throw new Error("A required attachment is unavailable or changed. The saved input was kept; re-upload the complete files before sending.");
        }
        const blob = await response.blob();
        files.push(new File([blob], meta.filename, { type: meta.mime_type }));
      }
      if (controller.signal.aborted) return;
      if (!draft?.current() || draft.store.getState().version !== draftVersion ||
        latestRestore.current !== restore || useSessionStore.getState().activeSessionId !== sessionId) {
        throw new Error("The conversation or draft changed. Clear the draft and restore the saved input again.");
      }
      restore(saved, files);
    } catch (err) {
      if (controller.signal.aborted) return;
      const fallback = dismissing ? "Couldn't dismiss saved input" : "Couldn't restore saved input";
      const message = err instanceof Error ? err.message : fallback;
      if (dismissing) {
        setDismissal({ notice: item, error: message });
      } else {
        setList((old) => ({ ...old, error: message }));
      }
    } finally {
      requests.current.delete(controller);
      if (!controller.signal.aborted) setBusy(null);
    }
  }

  function more() {
    if (!cursor || busy || pagination) return;
    const version = listVersion.current;
    setList((old) => old.pending || nextCursor(old) !== cursor ? old : { ...old, pending: { before: cursor, version } });
  }

  function retryLoading() {
    if (dismissal?.error) clearItemFeedback(dismissal.notice);
    setRefresh((n) => n + 1);
  }

  const error = dismissal?.error || list.error;
  const items = new Map<string, ClientSubmissionSummary>();
  for (const page of [list.first, ...list.additional.map(({ page }) => page)]) {
    for (const item of page.items) {
      if (!items.has(item.client_request_id)) items.set(item.client_request_id, item);
    }
  }
  if (dismissal && !items.has(dismissal.notice.client_request_id)) {
    items.set(dismissal.notice.client_request_id, dismissal.notice);
  }
  if (!token || (!items.size && !cursor && !error)) return null;
  const readingOrActing = !!busy || !!pagination;
  return <section aria-label="Saved inputs" className="shrink-0 max-h-72 overflow-auto px-6 py-2 text-sm">
    {Array.from(items.values(), (item) => {
      const native = item.execution.native_inputs ?? [];
      const savedInput = details[item.client_request_id]?.input;
      const waiting = native.some((input) => input.kind === "steer" && input.state === "not_sent") &&
        item.execution.state === "running";
      return <article key={item.client_request_id} className="mb-2 rounded-md border border-border p-3">
      <p className="font-medium">{item.operation === "compact" ? item.execution.state === "withdrawn" ? "Compaction not executed" : "Compaction result unknown" : waiting
        ? "Added to active turn · acceptance pending" : item.execution.state === "withdrawn" ? "Not executed · input saved" : "Execution result unknown"}</p>
      <p className="text-xs text-muted-foreground">{waiting ? "The input is owned by the active request. Runtime acceptance has not been confirmed."
        : item.operation === "compact" ? "Review the original operation. It cannot be restored or replayed automatically."
        : item.execution.state === "withdrawn"
        ? "This input was withdrawn before it ran. Restore it to review and send it again."
        : "Review the conversation and saved input before starting new work. It will not run again automatically."}</p>
      {item.execution.native_inputs?.some((input) => input.state === "unknown")
        ? <p className="text-xs text-muted-foreground">Native input acceptance is unconfirmed. A completed response does not resolve this uncertainty.</p>
        : item.execution.state === "unknown" && item.execution.native_inputs?.some((input) => input.state === "accepted")
          ? <p className="text-xs text-muted-foreground">Native input was accepted; model processing and the result remain unconfirmed.</p>
          : null}
      {item.execution.native_inputs?.some((input) => input.controller === "unknown") &&
        <p className="text-xs text-muted-foreground">A session preparation change is unconfirmed. Review the native history before continuing.</p>}
      <p className="my-2 whitespace-pre-wrap break-words">{item.preview || "Attachment-only input"}</p>
      {item.attachment_count > 0 && <p>{item.attachment_count} required attachment(s)</p>}
      {details[item.client_request_id] && <div className="my-2">
        {details[item.client_request_id].message && <p>{details[item.client_request_id].message}</p>}
        {savedInput?.kind === "message" && <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words">{savedInput.content}</pre>}
        {(savedInput?.kind === "message" ? savedInput.attachments : []).map((file) => <p key={file.id}>
          {file.filename} · {file.size} bytes
        </p>)}
      </div>}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="outline" disabled={readingOrActing} onClick={() => void action(item, "review")}>Review saved input</Button>
        {item.execution.recoverable_input && <Button size="sm" variant="outline" disabled={disabled || readingOrActing || !connected}
          onClick={() => void action(item, "restore")}>{busy === item.client_request_id ? "Loading…" : "Restore to input"}</Button>}
        {missingFiles[item.client_request_id] && details[item.client_request_id]?.execution.recoverable_input && <Button
          size="sm" variant="outline" disabled={disabled || readingOrActing || !connected}
          onClick={() => {
            clearItemFeedback(item);
            onRestore(details[item.client_request_id], []);
          }}>Restore text and re-attach files</Button>}
        <Button size="sm" variant="ghost" disabled={!!busy} onClick={() => void action(item, "dismiss")}>Dismiss notice</Button>
      </div>
    </article>; })}
    {cursor && <Button size="sm" variant="outline" disabled={readingOrActing} onClick={more}>Load more saved inputs</Button>}
    {error && <p role="alert">{error} <button type="button" className="underline" onClick={retryLoading}>Retry loading</button></p>}
  </section>;
}
