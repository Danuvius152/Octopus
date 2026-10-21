import { useEffect, useRef, useState } from "react";
import type { ClientSubmissionStatus, ClientSubmissionSummary, SubmissionRecoveryPage } from "../api";
import { accountFetch as fetch } from "../lib/accountFetch";
import { useSessionStore } from "../stores/sessionStore";
import { Button } from "./ui/button";

interface Props {
  sessionId: string;
  connected: boolean;
  disabled: boolean;
  onRestore: (saved: ClientSubmissionStatus, files: File[]) => void;
}

/** Reads saved input; only the composer can create a new execution. */
export function RecoverableInputs({ sessionId, connected, disabled, onRestore }: Props) {
  const token = useSessionStore((s) => s.token);
  const revision = useSessionStore((s) => s.dispatchStates[sessionId]?.revision);
  const generation = useSessionStore((s) => s.dispatchGeneration);
  const [page, setPage] = useState<SubmissionRecoveryPage>({ items: [] });
  const [details, setDetails] = useState<Record<string, ClientSubmissionStatus>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [missingFiles, setMissingFiles] = useState<Record<string, boolean>>({});
  const [refresh, setRefresh] = useState(0);
  const owner = useRef(0);
  const requests = useRef(new Set<AbortController>());
  const latestRestore = useRef(onRestore);
  latestRestore.current = onRestore;
  const api = `${window.location.origin}/api/sessions/${encodeURIComponent(sessionId)}/submissions`;

  useEffect(() => {
    const version = ++owner.current;
    const controller = new AbortController();
    const pending = requests.current;
    pending.add(controller);
    if (token && connected) {
      void fetch(api, { headers: { Authorization: `Bearer ${token}` }, signal: controller.signal })
        .then(async (res) => {
          if (!res.ok) throw new Error(`Couldn't load saved inputs (${res.status})`);
          const value = await res.json() as SubmissionRecoveryPage;
          if (!value || !Array.isArray(value.items)) throw new Error("Saved input status was invalid. Try loading it again.");
          return value;
        })
        .then((value) => {
          if (!controller.signal.aborted && owner.current === version) {
            setPage(value); setError("");
          }
        })
        .catch((err: unknown) => {
          if (!controller.signal.aborted && owner.current === version) setError(err instanceof Error ? err.message : "Couldn't load saved inputs");
        }).finally(() => pending.delete(controller));
    }
    return () => { controller.abort(); pending.delete(controller); };
  }, [api, token, connected, revision, generation, refresh]);

  useEffect(() => {
    const pending = requests.current;
    return () => { for (const request of pending) request.abort(); pending.clear(); };
  }, []);

  async function read<T>(url: string, controller: AbortController): Promise<T> {
    const response = await fetch(url, { headers: { Authorization: `Bearer ${token}` }, signal: controller.signal });
    if (!response.ok) throw new Error(`Couldn't read saved input (${response.status})`);
    return await response.json() as T;
  }

  async function action(item: ClientSubmissionSummary, operation: "review" | "restore" | "dismiss") {
    if (busy) return;
    const controller = new AbortController();
    requests.current.add(controller);
    const restore = onRestore;
    setBusy(item.client_request_id); setError("");
    try {
      const url = `${api}/${encodeURIComponent(item.client_request_id)}`;
      if (operation === "dismiss") {
        const response = await fetch(`${url}/recovery`, { method: "DELETE", headers: { Authorization: `Bearer ${token}` }, signal: controller.signal });
        if (!response.ok) throw new Error(`Couldn't dismiss saved input (${response.status})`);
        if (!controller.signal.aborted) setPage((old) => ({ ...old, items: old.items.filter((row) => row.client_request_id !== item.client_request_id) }));
        return;
      }
      const saved = await read<ClientSubmissionStatus>(url, controller);
      if (controller.signal.aborted) return;
      setDetails((old) => ({ ...old, [item.client_request_id]: saved }));
      if (operation === "review") return;
      if (!saved.execution.recoverable_input || !saved.input) throw new Error("This input cannot be restored. Review its current execution status.");
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
      if (latestRestore.current !== restore || useSessionStore.getState().activeSessionId !== sessionId) {
        throw new Error("The conversation or draft changed. Clear the draft and restore the saved input again.");
      }
      restore(saved, files);
    } catch (err) {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : "Couldn't restore saved input");
    } finally {
      requests.current.delete(controller);
      if (!controller.signal.aborted) setBusy(null);
    }
  }

  async function more() {
    if (!page.next_cursor || busy) return;
    const version = owner.current;
    const controller = new AbortController();
    requests.current.add(controller); setBusy("page");
    try {
      const next = await read<SubmissionRecoveryPage>(`${api}?before=${page.next_cursor}`, controller);
      if (!controller.signal.aborted && owner.current === version) setPage((old) => ({
        ...next, items: [...old.items, ...next.items.filter((row) => !old.items.some((existing) => existing.client_request_id === row.client_request_id))],
      }));
    } catch (err) {
      if (!controller.signal.aborted && owner.current === version) setError(err instanceof Error ? err.message : "Couldn't load saved inputs");
    } finally {
      requests.current.delete(controller);
      if (!controller.signal.aborted) setBusy(null);
    }
  }

  if (!token || (!page.items.length && !error)) return null;
  return <section aria-label="Saved inputs" className="shrink-0 max-h-72 overflow-auto px-6 py-2 text-sm">
    {page.items.map((item) => <article key={item.client_request_id} className="mb-2 rounded-md border border-border p-3">
      <p className="font-medium">{item.execution.state === "withdrawn" ? "Not executed · input saved" : "Execution result unknown"}</p>
      <p className="text-xs text-muted-foreground">{item.execution.state === "withdrawn"
        ? "This input was withdrawn before it ran. Restore it to review and send it again."
        : "Review the conversation and saved input before starting new work. It will not run again automatically."}</p>
      <p className="my-2 whitespace-pre-wrap break-words">{item.preview || "Attachment-only input"}</p>
      {item.attachment_count > 0 && <p>{item.attachment_count} required attachment(s)</p>}
      {details[item.client_request_id]?.input && <pre className="my-2 max-h-40 overflow-auto whitespace-pre-wrap break-words">{details[item.client_request_id].input?.content}</pre>}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="outline" disabled={!!busy} onClick={() => void action(item, "review")}>Review saved input</Button>
        {item.execution.recoverable_input && <Button size="sm" variant="outline" disabled={disabled || !!busy || !connected}
          onClick={() => void action(item, "restore")}>{busy === item.client_request_id ? "Loading…" : "Restore to input"}</Button>}
        {missingFiles[item.client_request_id] && details[item.client_request_id]?.execution.recoverable_input && <Button
          size="sm" variant="outline" disabled={disabled || !!busy || !connected}
          onClick={() => onRestore(details[item.client_request_id], [])}>Restore text and re-attach files</Button>}
        <Button size="sm" variant="ghost" disabled={!!busy} onClick={() => void action(item, "dismiss")}>Dismiss notice</Button>
      </div>
    </article>)}
    {page.next_cursor && <Button size="sm" variant="outline" disabled={!!busy} onClick={() => void more()}>Load more saved inputs</Button>}
    {error && <p role="alert">{error} <button type="button" className="underline" onClick={() => setRefresh((n) => n + 1)}>Retry loading</button></p>}
  </section>;
}
