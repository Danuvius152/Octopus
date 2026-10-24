import { useEffect, useRef, useState } from "react";
import type { WikiJobCard, WikiJobDetail, WikiJobOutput } from "../api";
import { controlWikiJob, loadWikiJobDetail, loadWikiJobOutput } from "../lib/wikiJobs";
import { useSessionStore } from "../stores/sessionStore";

export function WikiJobDetails({ card, sessionId, close, setUnavailable }: {
  card: WikiJobCard; sessionId: string; close: () => void; setUnavailable: (value: boolean) => void;
}) {
  const [attempt, setAttempt] = useState(card.attempt);
  const [detail, setDetail] = useState<WikiJobDetail | null>(null);
  const [output, setOutput] = useState<WikiJobOutput | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [paths, setPaths] = useState("");
  const [newPaths, setNewPaths] = useState("");
  const [refresh, setRefresh] = useState(0);
  const operations = useRef(new Map<string, string>());
  const reading = useRef(0);
  const token = useSessionStore((state) => state.token);
  const generation = useSessionStore((state) => state.dispatchGeneration);
  const { job_id, vault_id } = card;
  useEffect(() => {
    let active = true;
    const reads = reading;
    reads.current++;
    setOutput(null);
    setDetail(null);
    setBusy(false);
    void loadWikiJobDetail({ job_id, vault_id }, attempt).then((value) => {
      if (active) {
        if (value === null) setUnavailable(true);
        else { setDetail(value); setError(""); }
      }
    }).catch((failure) => { if (active) setError(String(failure.message)); });
    return () => { active = false; reads.current++; };
  }, [job_id, vault_id, attempt, token, generation, refresh, setUnavailable]);

  const more = async () => {
    if (!detail || detail.next_offset === null) return;
    setBusy(true);
    try {
      const page = await loadWikiJobDetail(card, attempt, detail.next_offset);
      if (page === null) setUnavailable(true);
      else setDetail({ ...page, items: [...detail.items, ...page.items], outputs: [...detail.outputs, ...page.outputs] });
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Could not load task details."); }
    finally { setBusy(false); }
  };
  const read = async (receipt: string, view: "history" | "current", more = false) => {
    const identity = ++reading.current;
    setBusy(true); setError("");
    try {
      const page = await loadWikiJobOutput(card, attempt, receipt, view, more ? output?.next_start ?? 0 : 0,
        more ? output?.revision : undefined);
      if (reading.current === identity) setOutput({ ...page, content: more ? (output?.content ?? "") + page.content : page.content });
    } catch (failure) { if (reading.current === identity) setError(failure instanceof Error ? failure.message : "Could not read output."); }
    finally { if (reading.current === identity) setBusy(false); }
  };
  const act = async (action: "verify" | "retry" | "readmit") => {
    const scope = action === "readmit" ? {
      paths: paths.split("\n").map((path) => path.trim()).filter(Boolean),
      ...(newPaths.trim() ? { creation: { purpose: "requested_knowledge_page" as const,
        paths: newPaths.split("\n").map((path) => path.trim()).filter(Boolean) } } : {}),
    } : {};
    const key = JSON.stringify([action, attempt, scope]);
    if (!operations.current.has(key)) operations.current.set(key, crypto.randomUUID());
    setBusy(true); setError("");
    try {
      await controlWikiJob(sessionId, { ...card, attempt }, action, { ...scope, operation_id: operations.current.get(key)! });
      operations.current.delete(key);
      setRefresh((value) => value + 1);
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Could not update the task."); }
    finally { setBusy(false); }
  };
  return <section role="dialog" aria-label="Wiki task details" className="mt-2 space-y-2 rounded border border-border p-3 text-xs">
    <div className="flex justify-between gap-2"><strong>Task details</strong><button onClick={close}>Close details</button></div>
    {detail && <>
      <label>Attempt <select aria-label="Maintenance attempt" value={attempt} disabled={busy} onChange={(event) => {
        setDetail(null); setAttempt(Number(event.target.value));
      }}>{detail.attempts.map((value) => <option key={value} value={value}>{value}{value === detail.current_attempt ? " (current)" : " (historical)"}</option>)}</select></label>
      {detail.error && <pre className="whitespace-pre-wrap break-words text-destructive">{detail.error}</pre>}
      {detail.items.map((item) => <div key={item.key}><p>{item.path} · {item.status}</p>
        {item.error && <pre className="whitespace-pre-wrap break-words text-destructive">{item.error}</pre>}
        {item.execution && <details><summary>Original execution evidence</summary><pre className="overflow-auto whitespace-pre-wrap break-words">{JSON.stringify(item.execution, null, 2)}</pre></details>}
      </div>)}
      {detail.outputs.map((saved) => <div key={saved.receipt_id} className="space-y-1 border-t border-border pt-2">
        <p>{saved.path} · {saved.operation} · {saved.status}{saved.attempt === null ? " · original attempt unknown" : ""}</p>
        <p className="whitespace-pre-wrap">{saved.reason}</p>
        {saved.current_path && <p>Current location: {saved.current_path}</p>}
        <div className="flex gap-3">
          <button disabled={busy} onClick={() => void read(saved.receipt_id, "history")} className="text-primary">Historical output</button>
          {saved.current_path && <button disabled={busy} onClick={() => void read(saved.receipt_id, "current")} className="text-primary">Open current text</button>}
        </div>
      </div>)}
      {detail.next_offset !== null && <button disabled={busy} onClick={() => void more()} className="text-primary">More task evidence</button>}
      {detail.actions.blocked_reason && <p>{detail.actions.blocked_reason}</p>}
      {attempt === detail.current_attempt && <div className="space-y-2 border-t border-border pt-2">
        {detail.actions.verify && <div><button disabled={busy} onClick={() => void act("verify")} className="text-primary">Verify saved result and index</button><p>Check the original evidence and rebuild indexing without running the model.</p></div>}
        {detail.actions.retry && <div><button disabled={busy} onClick={() => void act("retry")} className="text-primary">Continue unfinished work</button><p>Start only work known to be unfinished after the original execution retired.</p></div>}
        {detail.actions.readmit && <div className="space-y-1">
          <label>Existing note paths, one per line<textarea aria-label="Existing note paths" value={paths} onChange={(event) => setPaths(event.target.value)} className="block w-full border border-border" /></label>
          <label>Original unfinished new page paths, one per line<textarea aria-label="New page paths" value={newPaths} onChange={(event) => setNewPaths(event.target.value)} className="block w-full border border-border" /></label>
          <button disabled={busy} onClick={() => void act("readmit")} className="text-primary">Re-admit selected scope</button>
          <p>The owner explicitly grants a finite scope for the retained task. Unknown effects need verification first.</p>
        </div>}
      </div>}
    </>}
    {output && <div className="space-y-1 border-t border-border pt-2">
      <strong>{output.view === "history" ? "Historical output from this attempt" : "Current text"} · {output.path}</strong>
      <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words">{output.content}</pre>
      {output.next_start !== null && <button disabled={busy} onClick={() => void read(output.receipt_id, output.view, true)} className="text-primary">More output text</button>}
    </div>}
    {error && <p role="alert" className="whitespace-pre-wrap break-words text-destructive">{error}</p>}
  </section>;
}
