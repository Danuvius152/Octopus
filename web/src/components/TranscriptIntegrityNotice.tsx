import { useState } from "react";
import { useSessionStore } from "../stores/sessionStore";

export function TranscriptIntegrityNotice({ sessionId }: { sessionId: string }) {
  const [error, setError] = useState("");
  const [downloading, setDownloading] = useState(false);

  async function download() {
    setDownloading(true);
    setError("");
    try {
      const response = await fetch(`/api/sessions/${sessionId}/transcript-export`, {
        headers: { Authorization: `Bearer ${useSessionStore.getState().token}` },
      });
      if (!response.ok) throw new Error("Could not export this transcript. Try again.");
      const url = URL.createObjectURL(await response.blob());
      try {
        const link = document.createElement("a");
        link.href = url;
        link.download = "transcript.jsonl";
        link.click();
      } finally {
        URL.revokeObjectURL(url);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not export this transcript.");
    } finally {
      setDownloading(false);
    }
  }

  return <div role="alert" className="border-b border-border bg-muted/40 px-4 py-3 text-sm">
    <p>This transcript contains duplicate message sequence numbers. Its original rows
      are preserved. Conversation, paging and forks are paused until the history is reviewed.</p>
    <button type="button" className="mt-2 text-primary underline" disabled={downloading}
      onClick={() => void download()}>{downloading ? "Exporting…" : "Export all original rows"}</button>
    {error && <p className="mt-2 text-destructive">{error}</p>}
  </div>;
}
