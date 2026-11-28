import { useEffect, useEffectEvent, useLayoutEffect, useMemo, useRef, useState } from "react";
import { IconGitFork, IconX } from "@tabler/icons-react";
import type { components } from "../api/contracts";
import { accountFetch as fetch } from "../lib/accountFetch";
import { sessions as sessionList, type SessionObservation } from "../lib/sessionList";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
// Which user-role messages the user never typed (bg-task results, delegation
// replies, scheduled fires). Shared with the composer's history, because two
// copies of that list drift and the drift is invisible until a marker is added.
import { isAutoInjectedMessage } from "../lib/injectedTurns";
import { TranscriptIntegrityNotice } from "./TranscriptIntegrityNotice";
import { Button } from "./ui/button";
import { Input } from "./ui/input";

const API_URL = window.location.origin;
const REWIND_READ_TIMEOUT_MS = 30_000;

export interface ForkSideEffectSummary {
  file_edits: { path: string; turns: number }[];
  bg_tasks: {
    task_id: string | null;
    command: string | null;
    description: string | null;
    status: string;
  }[];
  other_tools: { label: string; count: number }[];
  counts: { total: number; file_edits: number; bg_tasks: number };
}

export interface ForkPreview {
  rewind_to_msg_seq: number;
  prefilled_prompt: string;
  side_effect_summary: ForkSideEffectSummary;
  revert: { available: boolean; refused_reason: string | null };
  can_fork: boolean;
}

interface UserMsg {
  seq: number;
  preview: string;
}

type HistoryPage = components["schemas"]["BrowserMessagePage"] & {
  transcript_integrity?: components["schemas"]["BrowserSessionDetail"]["transcript_integrity"];
};

type PickerPage =
  | { state: "loading"; before: number | null }
  | { state: "ready"; next: number | null }
  | { state: "failed"; before: number | null; error: string }
  | { state: "blocked" };

interface PickerState {
  messages: UserMsg[];
  page: PickerPage;
}

type PreviewRead =
  | { state: "loading" }
  | { state: "ready"; preview: ForkPreview }
  | { state: "failed"; error: string };

const blockedPicker: PickerState = { messages: [], page: { state: "blocked" } };

function observeRewindRead(sessionId: string, onRetired: () => void) {
  const tracked = sessionList.observe(sessionId);
  const controller = new AbortController();
  const deadline = AbortSignal.timeout(REWIND_READ_TIMEOUT_MS);
  const signal = AbortSignal.any([controller.signal, deadline]);
  const unsubscribe = useSessionStore.subscribe(current);

  function finish() {
    unsubscribe();
    tracked.finish();
  }

  function cancel() {
    finish();
    controller.abort();
  }

  function current() {
    if (controller.signal.aborted) return false;
    if (sessionList.current(tracked.observation)) return true;
    cancel();
    onRetired();
    return false;
  }

  return { signal, deadline, current, finish, cancel };
}

async function readRewindFailure(response: Response) {
  const detail = (await response.json().catch(() => null))?.detail;
  const message = typeof detail === "string" ? detail : detail?.message ?? detail?.reason;
  return {
    message: typeof message === "string" ? message : null,
    duplicateSequences: response.status === 409 &&
      (detail?.code === "duplicate_message_sequences" || detail?.reason === "duplicate_message_sequences"),
  };
}

/**
 * Presentational confirm view (session-rewind.md §5.6.2). Pure — takes a
 * fetched preview + controlled checkbox/label, renders the three-class
 * side-effect summary and the single revert affordance (disabled with the
 * preflight reason as a tooltip when revert isn't available). Exported so the
 * unit test can drive it without mocking the fetch flow.
 */
export function ForkConfirmView({
  parentName,
  preview,
  revertChecked,
  onRevertChange,
  label,
  onLabelChange,
}: {
  parentName: string;
  preview: ForkPreview;
  revertChecked: boolean;
  onRevertChange: (v: boolean) => void;
  label: string;
  onLabelChange: (v: string) => void;
}) {
  const s = preview.side_effect_summary;
  const revertAvailable = preview.revert.available;
  const m = preview.rewind_to_msg_seq;
  return (
    <div className="fork-confirm space-y-4" data-testid="fork-confirm">
      <p className="text-sm text-muted-foreground">
        Fork{" "}
        <span className="font-medium text-foreground">{parentName}</span> at
        message <span className="font-medium text-foreground">#{m}</span> —
        rewind to before it and redo it.
      </p>

      {/* Files modified + the single revert affordance */}
      <section className="fork-files rounded-lg border border-border bg-card p-3">
        <div className="flex items-center justify-between">
          <h4 className="text-xs font-semibold text-foreground">
            Files modified ({s.file_edits.length})
          </h4>
          <label
            className={`flex items-center gap-1.5 text-xs ${
              revertAvailable
                ? "text-foreground"
                : "text-muted-foreground/60 cursor-not-allowed"
            }`}
            title={
              revertAvailable
                ? "Restore the working tree to its fork-point state"
                : preview.revert.refused_reason ?? "Revert unavailable"
            }
            data-testid="fork-revert-label"
          >
            <input
              type="checkbox"
              data-testid="fork-revert-checkbox"
              checked={revertChecked && revertAvailable}
              disabled={!revertAvailable}
              onChange={(e) => onRevertChange(e.target.checked)}
            />
            Revert to fork-point state
          </label>
        </div>
        {s.file_edits.length === 0 ? (
          <p className="mt-1 text-xs text-muted-foreground/70">None</p>
        ) : (
          <ul className="mt-1.5 space-y-0.5">
            {s.file_edits.map((f) => (
              <li
                key={f.path}
                className="flex justify-between font-mono text-[11px] text-muted-foreground"
              >
                <span className="truncate">{f.path}</span>
                <span className="shrink-0 pl-2">
                  ({f.turns} turn{f.turns === 1 ? "" : "s"})
                </span>
              </li>
            ))}
          </ul>
        )}
        {!revertAvailable && preview.revert.refused_reason && (
          <p
            className="mt-1.5 text-[11px] text-muted-foreground/70"
            data-testid="fork-revert-reason"
          >
            Revert unavailable: {preview.revert.refused_reason}
          </p>
        )}
      </section>

      {s.bg_tasks.length > 0 && (
        <section className="fork-bg rounded-lg border border-border bg-card p-3">
          <h4 className="text-xs font-semibold text-foreground">
            Background tasks ({s.bg_tasks.length})
          </h4>
          <ul className="mt-1.5 space-y-0.5">
            {s.bg_tasks.map((t, i) => (
              <li
                key={t.task_id ?? i}
                className="flex justify-between text-[11px] text-muted-foreground"
              >
                <span className="truncate font-mono">
                  {t.command ?? t.description ?? "bg task"}
                </span>
                <span className="shrink-0 pl-2">{t.status}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {s.other_tools.length > 0 && (
        <section className="fork-other rounded-lg border border-border bg-card p-3">
          <h4 className="text-xs font-semibold text-foreground">
            Other tool activity (not revertible)
          </h4>
          <ul className="mt-1.5 space-y-0.5">
            {s.other_tools.map((o) => (
              <li
                key={o.label}
                className="text-[11px] text-muted-foreground"
              >
                {o.count} {o.label}
              </li>
            ))}
          </ul>
        </section>
      )}

      <div>
        <label className="text-xs text-muted-foreground">Label (optional)</label>
        <Input
          className="mt-1 h-9 text-sm"
          value={label}
          onChange={(e) => onLabelChange(e.target.value)}
          placeholder="Fork label"
        />
      </div>
    </div>
  );
}

/**
 * Full fork flow (session-rewind.md §5.6.2 / §6.1-6.2). Self-sufficient:
 * pages through the transcript for the picker and fetches `/fork-preview`
 * for the chosen message before POSTing `/fork`. When
 * `initialSeq` is given (the per-message "Fork from here" button) it jumps
 * straight to the confirm step.
 */
export function ForkDialog({
  sessionId,
  parentName,
  initialSeq,
  onClose,
  onForked,
}: {
  sessionId: string;
  parentName: string;
  initialSeq?: number;
  onClose: () => void;
  onForked: (fork: SessionInfo, observation: SessionObservation) => void;
}) {
  const token = useSessionStore((s) => s.token);
  const headers = useMemo(
    () => ({ "Content-Type": "application/json", Authorization: `Bearer ${token}` }),
    [token]
  );

  const [picker, setPicker] = useState<PickerState>({
    messages: [], page: { state: "loading", before: null },
  });
  const dialogRead = useRef<ReturnType<typeof observeRewindRead> | null>(null);
  const [selectedSeq, setSelectedSeq] = useState<number | null>(
    initialSeq ?? null
  );
  const [previewRead, setPreviewRead] = useState<PreviewRead>({ state: "loading" });
  const preview = previewRead.state === "ready" ? previewRead.preview : null;
  const [revertChecked, setRevertChecked] = useState(false);
  const [label, setLabel] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const foreground = useRef<{ active: boolean } | null>(null);
  const closeOnLeave = useEffectEvent(() => onClose());
  useLayoutEffect(() => {
    const account = useSessionStore.getState().accountGeneration;
    const lifetime = { active: true };
    foreground.current = lifetime;
    const check = (state: ReturnType<typeof useSessionStore.getState>) => {
      if (lifetime.active && (state.activeSessionId !== sessionId || state.mainView !== "chat" ||
        state.accountGeneration !== account)) {
        lifetime.active = false;
        dialogRead.current?.cancel();
        closeOnLeave();
      }
    };
    const unsubscribe = useSessionStore.subscribe(check);
    check(useSessionStore.getState());
    return () => {
      lifetime.active = false;
      dialogRead.current?.cancel();
      unsubscribe();
    };
  }, [sessionId]);
  const close = () => {
    if (foreground.current) foreground.current.active = false;
    dialogRead.current?.cancel();
    onClose();
  };

  const { page, messages: userMsgs } = picker;
  useEffect(() => {
    const lifetime = foreground.current;
    if (selectedSeq !== null || page.state !== "loading" || !lifetime?.active) return;
    const { before } = page;
    const fail = (error: string) => {
      if (lifetime.active) setPicker((value) => ({ ...value, page: { state: "failed", before, error } }));
    };
    const read = observeRewindRead(sessionId, () => fail("History connection or service changed. Retry this page."));
    dialogRead.current = read;
    const { signal, deadline } = read;
    const current = () => lifetime.active && read.current();
    const path = `${API_URL}/api/sessions/${encodeURIComponent(sessionId)}`;
    const url = before === null ? `${path}?view=browser` : `${path}/messages?before_seq=${before}&view=browser`;
    (async () => {
      try {
        const res = await fetch(url, { headers, signal });
        if (!current()) return;
        if (!res.ok) {
          const { message, duplicateSequences } = await readRewindFailure(res);
          if (!current()) return;
          signal.throwIfAborted();
          if (duplicateSequences) setPicker(blockedPicker);
          else fail(`Could not load rewind messages (HTTP ${res.status}).${message ? ` ${message}` : ""}`);
          return;
        }
        const data: HistoryPage | null = await res.json();
        if (!current()) return;
        signal.throwIfAborted();
        if (data?.transcript_integrity === "duplicate_seq") {
          setPicker(blockedPicker);
          return;
        }
        if (!Array.isArray(data?.messages) || typeof data.has_more_messages !== "boolean") {
          return fail("Could not load rewind messages. Invalid history page.");
        }
        // The cursor belongs to the original page, including rows the picker hides.
        const next = data.has_more_messages ? data.oldest_loaded_seq : null;
        if (data.has_more_messages &&
          (typeof next !== "number" || !Number.isInteger(next) || next <= 0 || (before !== null && next >= before))) {
          return fail("Could not load rewind messages. Invalid history cursor.");
        }
        const rows: UserMsg[] = [];
        for (const message of data.messages) {
          if (typeof message?.seq !== "number" || message.role !== "user" || message.type !== "text" ||
            isAutoInjectedMessage({ content: message.content, tool_input: message.tool_input ?? undefined })) continue;
          rows.push({
            seq: message.seq,
            preview: typeof message.content === "string" ? message.content.split("\n", 1)[0].slice(0, 60) : "(message)",
          });
        }
        setPicker((value) => {
          const candidates = new Map(value.messages.map((message) => [message.seq, message]));
          for (const row of rows) {
            if (!candidates.has(row.seq)) candidates.set(row.seq, row);
          }
          return {
            messages: [...candidates.values()].sort((a, b) => a.seq - b.seq),
            page: { state: "ready", next: next ?? null },
          };
        });
      } catch (error) {
        if (!current()) return;
        if (deadline.aborted) fail("Loading rewind messages timed out.");
        else if (error instanceof SyntaxError) fail("Could not load rewind messages. Invalid history page.");
        else fail("Network error while loading rewind messages.");
      } finally {
        read.finish();
        if (dialogRead.current === read) dialogRead.current = null;
      }
    })();
    return () => {
      read.cancel();
    };
  }, [page, selectedSeq, sessionId, headers]);

  const loadCandidatePage = (before: number | null) => {
    setPicker((value) => ({ ...value, page: { state: "loading", before } }));
  };

  // Preview reads have the same lifetime as candidate reads and can be retried.
  useEffect(() => {
    const lifetime = foreground.current;
    if (selectedSeq === null || previewRead.state !== "loading" || !lifetime?.active) return;
    const fail = (error: string) => {
      if (lifetime.active) setPreviewRead({ state: "failed", error });
    };
    const read = observeRewindRead(sessionId, () => fail("History connection or service changed. Retry the preview."));
    dialogRead.current = read;
    const { signal, deadline } = read;
    const current = () => lifetime.active && read.current();
    (async () => {
      try {
        const res = await fetch(
          `${API_URL}/api/sessions/${sessionId}/fork-preview?rewind_to_msg_seq=${selectedSeq}`,
          { headers, signal }
        );
        if (!current()) return;
        if (!res.ok) {
          if (res.status === 409) {
            const { duplicateSequences } = await readRewindFailure(res);
            if (!current()) return;
            signal.throwIfAborted();
            if (duplicateSequences) {
              setPicker(blockedPicker);
              return;
            }
          }
          fail("Could not load fork preview.");
          return;
        }
        const data: ForkPreview = await res.json();
        if (!current()) return;
        signal.throwIfAborted();
        setPreviewRead({ state: "ready", preview: data });
        setRevertChecked(data.revert.available);
      } catch {
        if (current()) fail(deadline.aborted ? "Preview loading timed out." : "Could not load fork preview.");
      } finally {
        read.finish();
        if (dialogRead.current === read) dialogRead.current = null;
      }
    })();
    return () => {
      read.cancel();
    };
  }, [selectedSeq, sessionId, headers, previewRead.state]);

  const createFork = async () => {
    const lifetime = foreground.current;
    if (selectedSeq === null || !preview || !lifetime?.active || creating || page.state === "blocked") return;
    const tracked = sessionList.observe(sessionId, useSessionStore.getState(), "archive");
    const { observation } = tracked;
    const ownsDialog = () => lifetime.active && foreground.current === lifetime && sessionList.accountCurrent(observation);
    setCreating(true);
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/sessions/${sessionId}/fork`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          rewind_to_msg_seq: selectedSeq,
          revert_files: revertChecked && preview.revert.available,
          label: label.trim() || null,
        }),
      });
      if (!res.ok) {
        const { duplicateSequences, message } = await readRewindFailure(res);
        if (!ownsDialog()) return;
        if (!duplicateSequences) setError(message ?? "Fork failed.");
        else if (sessionList.current(observation)) setPicker(blockedPicker);
        else setError("This rewind was refused. You can try again.");
        return;
      }
      const fork: SessionInfo = await res.json();
      const applied = await sessionList.created(fork, observation, { archivedId: sessionId });
      if (!ownsDialog()) return;
      close();
      if (applied && sessionList.current(observation)) onForked(fork, observation);
    } catch {
      if (ownsDialog()) setError("Fork failed.");
    } finally {
      tracked.finish();
      if (ownsDialog()) setCreating(false);
    }
  };

  const inConfirm = selectedSeq !== null;
  const blocked = page.state === "blocked";
  const showConfirm = inConfirm && !blocked;

  return (
    <div
      className="fork-dialog-overlay fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={close}
    >
      <div
        className="fork-dialog w-full max-w-md rounded-xl border-[0.7px] border-border bg-background p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
        data-testid="fork-dialog"
      >
        <div className="mb-3 flex items-center gap-2">
          <IconGitFork size={18} className="text-primary" />
          <h3 className="flex-1 text-base font-semibold text-foreground">
            {inConfirm ? "Rewind to this message" : "Rewind to a message"}
          </h3>
          <button
            type="button"
            onClick={close}
            className="text-muted-foreground hover:text-foreground"
            aria-label="Close"
          >
            <IconX size={16} />
          </button>
        </div>

        {blocked && <TranscriptIntegrityNotice sessionId={sessionId} />}
        {!inConfirm && !blocked && (
          <div className="fork-picker" data-testid="fork-picker">
            <p className="mb-2 text-xs text-muted-foreground">
              Pick a user message to rewind to and redo:
            </p>
            {page.state === "loading" && (
              <p role="status" className="text-xs text-muted-foreground">
                {page.before === null ? "Loading rewind messages…" : "Loading earlier messages…"}
              </p>
            )}
            {page.state === "failed" && (
              <div className="mb-2 space-y-1">
                <p role="alert" className="text-xs text-destructive">{page.error}</p>
                <Button variant="outline" size="sm" onClick={() => loadCandidatePage(page.before)}>Retry</Button>
              </div>
            )}
            {page.state === "ready" && page.next !== null && (
              <Button variant="outline" size="sm" className="mb-2" onClick={() => loadCandidatePage(page.next)}>
                Load earlier messages
              </Button>
            )}
            {page.state === "ready" && page.next === null && userMsgs.length === 0 && (
              <p className="text-xs italic text-muted-foreground/60">
                No user messages to rewind to.
              </p>
            )}
            {/* Scrollable list — long conversations can have many turns. */}
            <div className="max-h-[50vh] space-y-1 overflow-y-auto pr-1">
              {userMsgs.map((u) => (
                <button
                  key={u.seq}
                  type="button"
                  data-fork-seq={u.seq}
                  className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm text-foreground hover:bg-accent"
                  onClick={() => setSelectedSeq(u.seq)}
                >
                  <span className="font-mono text-[11px] text-muted-foreground">
                    #{u.seq}
                  </span>
                  <span className="truncate">{u.preview}</span>
                </button>
              ))}
            </div>
          </div>
        )}

        {showConfirm && previewRead.state === "loading" && (
          <p role="status" className="text-sm text-muted-foreground">Loading…</p>
        )}

        {showConfirm && previewRead.state === "failed" && (
          <div className="space-y-1">
            <p role="alert" className="text-xs text-destructive">{previewRead.error}</p>
            <Button variant="outline" size="sm" onClick={() => setPreviewRead({ state: "loading" })}>Retry</Button>
          </div>
        )}

        {showConfirm && preview && (
          <ForkConfirmView
            parentName={parentName}
            preview={preview}
            revertChecked={revertChecked}
            onRevertChange={setRevertChecked}
            label={label}
            onLabelChange={setLabel}
          />
        )}

        {error && (
          <p role="alert" className="mt-3 text-xs text-destructive" data-testid="fork-error">
            {error}
          </p>
        )}

        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={close}>
            Cancel
          </Button>
          {showConfirm && (
            <Button
              size="sm"
              className="btn-create-fork"
              disabled={!preview || creating}
              onClick={createFork}
            >
              {creating ? "Rewinding…" : "Rewind"}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
