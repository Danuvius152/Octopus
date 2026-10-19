import { accountFetch as fetch } from "../lib/accountFetch";
import { useEffect, useRef } from "react";
import { IconWorldSearch, IconX, IconCheck, IconAlertTriangle } from "@tabler/icons-react";
import { useSessionStore, type ResearchJob } from "../stores/sessionStore";

const API = window.location.origin;
const EMPTY_JOBS: ResearchJob[] = [];

const PHASES = ["scope", "search", "verify", "synthesize", "done"];
const PHASE_LABEL: Record<string, string> = {
  scope: "Planning angles",
  search: "Searching the web",
  verify: "Verifying claims",
  synthesize: "Writing the report",
  done: "Done",
};

/** Live deep-research progress for the active session (native-deep-research.md
 * §7). The final cited report arrives as a normal injected turn; this card is
 * the progress/affordance surface. Terminal jobs linger briefly as a result. */
export function ResearchCard({ sessionId }: { sessionId: string }) {
  const token = useSessionStore((s) => s.token);
  const jobs = useSessionStore((s) => s.research[sessionId]) ?? EMPTY_JOBS;
  const confirmations = useRef(new Set<string>());
  useEffect(() => {
    if (!token) return;
    for (const job of jobs) {
      const receipt = job.delivery_receipt;
      if (!receipt || receipt.browser_confirmed_version === receipt.version) continue;
      const key = `${job.id}:${receipt.request_id}:${receipt.version}`;
      if (confirmations.current.has(key)) continue;
      confirmations.current.add(key);
      void fetch(`${API}/api/sessions/${sessionId}/research/${job.id}/seen`, {
        method: "POST", headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: receipt.request_id, version: receipt.version }),
      }).then(async (response) => {
        if (!response.ok) throw new Error("Confirmation was not accepted");
        const body = await response.json();
        const store = useSessionStore.getState();
        const current = store.research[sessionId]?.find((item) => item.id === job.id);
        if (current?.delivery_receipt?.request_id === receipt.request_id && current.delivery_receipt.version === receipt.version) {
          store.upsertResearch(sessionId, { id: job.id, delivery_receipt: body.delivery_receipt });
        }
      }).catch(() => { confirmations.current.delete(key); });
    }
  }, [jobs, sessionId, token]);
  if (jobs.length === 0) return null;

  const cancel = async (job: ResearchJob) => {
    try {
      await fetch(`${API}/api/sessions/${sessionId}/research/${job.id}/cancel`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      });
    } catch {
      /* best-effort; the WS event will reconcile */
    }
  };

  return (
    <div className="research-cards flex flex-col gap-2">
      {jobs.map((job) => {
        const running = job.status === "running";
        const phaseIdx = PHASES.indexOf(job.phase ?? "scope");
        return (
          <div
            key={job.id}
            data-research-id={job.id}
            data-status={job.status}
            className="research-card rounded-lg border border-border bg-muted/30 px-3 py-2 text-sm"
          >
            <div className="flex items-center gap-2">
              <IconWorldSearch
                size={15}
                className={running ? "text-primary animate-pulse" : "text-muted-foreground"}
              />
              <span className="research-question flex-1 truncate font-medium text-foreground">
                {job.question}
              </span>
              {job.status === "completed" && (
                <IconCheck size={15} className="text-emerald-600 shrink-0" />
              )}
              {(job.status === "failed" ||
                job.status === "cancelled" ||
                job.status === "interrupted") && (
                <IconAlertTriangle size={15} className="text-destructive shrink-0" />
              )}
              {running && (
                <button
                  type="button"
                  className="btn-research-cancel inline-flex h-5 w-5 items-center justify-center rounded text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                  onClick={() => cancel(job)}
                  title="Cancel research"
                  aria-label="Cancel research"
                >
                  <IconX size={13} />
                </button>
              )}
            </div>
            <div className="research-status mt-1 text-xs text-muted-foreground">
              {running ? (
                <span className="research-phase">
                  {PHASE_LABEL[job.phase ?? "scope"] ?? job.phase}
                  {job.detail ? ` — ${job.detail}` : "…"}
                  {phaseIdx >= 0 ? `  (${phaseIdx + 1}/${PHASES.length})` : ""}
                </span>
              ) : job.status === "completed" ? (
                <span>
                  {job.delivery_receipt
                    ? ({ queued: "Report queued for follow-up", processing: "Follow-up is running", processed: "Follow-up processed",
                      failed: "Follow-up failed; report retained", recording_unconfirmed: "Follow-up saving is unconfirmed",
                      unknown: "Follow-up state is unknown; inspect the original request" })[job.delivery_receipt.stage]
                    : job.injection_status === "delivered"
                    ? "Report accepted for follow-up; processing unconfirmed"
                    : job.injection_status === "failed"
                      ? `Report retained as material${job.error ? ` — ${job.error}` : ""}`
                      : "Report ready; delivery pending"}
                  {typeof job.verified === "number"
                    ? ` · ${job.verified} verified finding${job.verified === 1 ? "" : "s"}`
                    : ""}
                  {typeof job.unverified === "number" && job.unverified > 0 ? ` · ${job.unverified} unverified` : ""}
                  {job.delivery_receipt?.browser_confirmed_version === job.delivery_receipt?.version && job.delivery_receipt
                    ? " · Browser received this status" : ""}
                  {job.sources && job.sources.length
                    ? ` · ${job.sources.length} source${job.sources.length === 1 ? "" : "s"}`
                    : ""}
                </span>
              ) : (
                <span className="text-destructive">
                  {job.status === "cancelled"
                    ? "Cancelled."
                    : job.error || "Research failed."}
                </span>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
