import { accountFetch as fetch } from "../lib/accountFetch";
import { useCallback, useEffect, useRef } from "react";
import { newSubmissionId, type SubmissionReply } from "../lib/submission";
import {
  addSessionInfo,
  loadSessionSnapshot,
  loadBgTaskSnapshot,
  hydrateSession,
  sessionInfoFromDetail,
} from "../lib/hydrateSession";
import { readRuntimeMetadata } from "../lib/toolIdentity";
import { refreshSchedules } from "../lib/refreshSchedules";
import { refreshWikiJobs } from "../lib/wikiJobs";
import type { WikiMessageMode } from "../lib/wikiComposer";
import {
  useSessionStore,
  type Agent,
  type Application,
  type BgTask,
  type Message,
  type PendingQuestion,
  type ResearchJob,
  type SessionInfo,
  type SessionStatus,
  type SubagentRun,
} from "../stores/sessionStore";

type BgTaskStatus = BgTask["status"];

const WS_PROTOCOL = window.location.protocol === "https:" ? "wss:" : "ws:";
const WS_URL = `${WS_PROTOCOL}//${window.location.host}/ws`;

// Access store actions directly (stable references, no re-renders)
const getState = () => useSessionStore.getState();

/** Snapshot-baseline dedup for WS message events.
 *
 * Events that correspond to a DB message row carry `seq`
 * (server-assigned). After loading a snapshot the store sets
 * `lastAppliedSeq[sessionId]` to `next_message_seq - 1`; any
 * subsequently-broadcast event with `seq <= baseline` is already in
 * the snapshot and must be dropped to avoid double-rendering.
 *
 * Events without `seq` (such as status) are ephemeral and always applied.
 *
 * Exported only so the unit tests can exercise the guard directly;
 * call sites (handleWsMessage) consume it internally.
 */
export function shouldApplyWsEvent(
  seq: number | null | undefined,
  baseline: number | undefined
): boolean {
  if (typeof seq !== "number") return true;
  const b = typeof baseline === "number" ? baseline : -1;
  return seq > b;
}

/** Exported for tests: the whole switch is a side-effecting reducer over the
 * store, and the token cases are ones a regression would silently sign
 * everyone out (or fail to). */
export function handleWsMessage(data: Record<string, unknown>) {
  const {
    addMessage: addMessageRaw,
    updateSessionStatus,
    applyDispatchState,
    addPendingQuestion,
    removePendingQuestion,
    setLastAppliedSeq,
    lastAppliedSeq,
    appendStreamingText,
    clearStreamingText,
    upsertSubagent,
  } = getState();
  const sessionId = data.session_id as string;
  const type = data.type as string;

  const seq = typeof data.seq === "number" ? (data.seq as number) : null;
  // The time the server stamped on the row this event is (`_tag_persisted`).
  // Injected once here rather than spelled out in each `addMessage` call below:
  // every event that represents a stored message carries it, and a shape that
  // forgot to copy it would silently lose the hover timestamp until a reload.
  const addMessage: typeof addMessageRaw = (sid, msg) =>
    addMessageRaw(sid, {
      ...msg,
      runtime_metadata: msg.runtime_metadata ?? readRuntimeMetadata(data.runtime_metadata),
      seq: msg.seq ?? seq ?? undefined,
      created_at:
        msg.created_at ??
        (typeof data.created_at === "string"
          ? (data.created_at as string)
          : undefined),
    });
  if (seq !== null && sessionId) {
    if (!shouldApplyWsEvent(seq, lastAppliedSeq[sessionId])) {
      return; // already in snapshot; ignore to avoid duplicate
    }
    setLastAppliedSeq(sessionId, seq);
  }

  switch (type) {
    case "attempt_started":
      if (typeof data.request_id === "string" && typeof data.attempt_id === "string") {
        clearStreamingText(sessionId);
        getState().setActiveAttempt(sessionId, { request_id: data.request_id, attempt_id: data.attempt_id });
      }
      break;
    case "wiki_job_changed":
      if (typeof data.vault_id !== "string" || typeof data.job_id !== "string" ||
        !Number.isInteger(data.state_version) || Number(data.state_version) < 1) break;
      getState().invalidateWikiJob(sessionId, { vault_id: data.vault_id, job_id: data.job_id, state_version: Number(data.state_version) });
      if (sessionId === getState().activeSessionId) void refreshWikiJobs(sessionId).catch(() => {});
      break;
    case "dispatch_state":
      applyDispatchState(sessionId, data.dispatch_state);
      break;

    case "recording_failed":
      clearStreamingText(sessionId);
      applyDispatchState(sessionId, data.dispatch_state);
      break;

    case "queued":
    case "dequeued":
      applyDispatchState(sessionId, data.dispatch_state);
      break;

    case "delivery_notice":
      addMessage(sessionId, { role: "system", type: "notice", content: data.content ?? data.message });
      break;

    // Partial text, still being written. `addMessage` drops the buffer when
    // the completed block lands, so there's nothing to clear here.
    case "assistant_delta":
      appendStreamingText(sessionId, data.content as string);
      break;

    // A schedule was created, changed or deleted — by this browser, another
    // tab, or an agent using its own schedule tool (schedule-tool.md §6).
    // The payload says only that the list moved; we refetch it.
    case "schedules_changed":
      void refreshSchedules();
      break;

    case "assistant_text":
      addMessage(sessionId, {
        role: "assistant",
        type: "text",
        content: data.content as string,
      });
      break;

    // A sub-agent the model spawned inside this turn (native-subagents.md).
    // Live state only — the Task / collab tool call in `messages` is the
    // durable record, so nothing is added to the transcript here.
    case "subagent":
      upsertSubagent(sessionId, {
        task_id: data.task_id as string,
        tool_call_id: (data.tool_call_id as string) ?? null,
        tool_use_id: (data.tool_use_id as string) ?? null,
        status: (data.status as SubagentRun["status"]) ?? "running",
        stop_reason: (data.stop_reason as string) ?? null,
        name: (data.name as string) ?? "",
        description: (data.description as string) ?? "",
        prompt: (data.prompt as string) ?? "",
        summary: (data.summary as string) ?? "",
        tokens: (data.tokens as number) ?? null,
        tool_uses: (data.tool_uses as number) ?? null,
        duration_ms: (data.duration_ms as number) ?? null,
        // The trail is built in the store from each step as it arrives; the
        // wire carries the step, not the history.
        steps: [],
      });
      break;

    case "tool_use": {
      const toolName = data.tool as string;
      const toolInput = data.input as Record<string, unknown>;
      addMessage(sessionId, {
        role: "assistant",
        type: "tool_use",
        tool_name: toolName,
        tool_input: toolInput,
        tool_use_id: data.tool_use_id as string,
      });
      break;
    }

    case "tool_result":
      addMessage(sessionId, {
        role: "tool",
        type: "tool_result",
        content: data.output as string,
        tool_use_id: data.tool_use_id as string,
        is_error: data.is_error as boolean,
      });
      break;

    case "question_request": {
      const questionId = data.question_id as string;
      if (getState().closedQuestionIds[sessionId]?.[questionId]) break;
      const questions = (data.questions as PendingQuestion["questions"]) || [];
      addMessage(sessionId, {
        role: "assistant",
        type: "question_request",
        tool_name: "AskUserQuestion",
        tool_input: { questions, deadline: data.deadline ?? null },
        tool_use_id: questionId,
      });
      addPendingQuestion(sessionId, {
        question_id: questionId,
        questions,
        deadline: data.deadline as string | null | undefined,
        request_id: data.request_id as string | null | undefined,
        attempt_id: data.attempt_id as string | null | undefined,
      });
      break;
    }

    case "question_answer": {
      const questionId = data.question_id as string;
      addMessage(sessionId, {
        role: "user",
        type: "question_answer",
        content: data.content as string,
        tool_use_id: questionId,
      });
      removePendingQuestion(sessionId, questionId);
      break;
    }

    case "question_closed": {
      const questionId = data.question_id as string;
      addMessage(sessionId, {
        role: "system",
        type: "question_closed",
        content: data.message as string,
        tool_use_id: questionId,
        tool_input: { reason: data.reason },
      });
      removePendingQuestion(sessionId, questionId);
      break;
    }

    case "status":
      updateSessionStatus(sessionId, data.status as SessionStatus);
      break;

    case "recovery":
      addMessage(sessionId, { role: "system", type: "recovery", content: data.content ?? data.message });
      break;

    case "result":
      if (getState().activeAttempts[sessionId]?.request_id === data.request_id) getState().setActiveAttempt(sessionId, null);
      // A turn can end with no completed text block at all — an interrupt, a
      // failure, or a turn that only ran tools. Whatever was streaming is not
      // going to be superseded by a real message, so drop it here.
      clearStreamingText(sessionId);
      addMessage(sessionId, {
        role: "system",
        type: "result",
        content: data.content ?? data.message,
        is_error: data.is_error as boolean | undefined,
        tool_input: data.tool_input as Message["tool_input"],
        cost: data.cost as number,
        session_id: data.claude_session_id as string,
      });
      if (data.code === "auth_expired") {
        const t = getState().token;
        if (t) void fetch(`${window.location.origin}/api/credentials`, {
          headers: { Authorization: `Bearer ${t}` },
        }).then((r) => r.ok ? r.json() : null)
          .then((creds) => creds && getState().setCredentials(creds)).catch(() => {});
      }
      break;

    // A message handed to the turn already running, rather than queued behind
    // it. Rendered immediately so you can see it landed mid-turn.
    case "steered":
      addMessage(sessionId, {
        role: "user",
        type: "text",
        content: data.content as string,
        steered: true,
        tool_input: data.tool_input as Message["tool_input"],
        seq: seq ?? undefined,
      });
      break;

    case "user_message":
      addMessage(sessionId, {
        role: "user",
        type: "text",
        content: data.content as string,
        attachments: (data.attachments as Message["attachments"]) ?? undefined,
        tool_input: data.tool_input as Message["tool_input"],
        // Carry the row seq so "Fork from here" can target a just-sent
        // message without waiting for a detail reload (the event always
        // includes it for persisted rows).
        seq: seq ?? undefined,
      });
      break;

    case "error":
      clearStreamingText(sessionId);
      addMessage(sessionId || "__global", {
        role: "system",
        type: "error",
        content: data.message as string,
      });
      // A mid-turn 401 flagged the bound credential needs_reconnect server-side
      // (harness-credential-reauth.md §6). Refetch credentials so the Harness
      // sidebar lights up its "Re-authorize" badge without a manual reload.
      if (data.code === "auth_expired") {
        const t = getState().token;
        if (t) {
          fetch(`${window.location.origin}/api/credentials`, {
            headers: { Authorization: `Bearer ${t}` },
          })
            .then((r) => (r.ok ? r.json() : null))
            .then((creds) => creds && getState().setCredentials(creds))
            .catch(() => {});
        }
      }
      break;

    // Applications (applications.md §4/§7). Global events — no session_id,
    // because the sidebar shows applications regardless of which session is
    // open. The row comes down whole, so the store just mirrors it.
    case "application_created":
    case "application_updated": {
      const row = data.application as Application | undefined;
      if (!row) break;
      const before = getState().applications.find((a) => a.id === row.id);
      const { mainView, activeApplicationId } = getState();
      // A build that failed where nobody could see it: an unpinned app keeps
      // a place in the sidebar until it's opened (sidebar-pins.md §5).
      if (
        row.status === "failed" &&
        before?.status !== "failed" &&
        !(mainView === "application" && activeApplicationId === row.id)
      ) {
        getState().noteApplicationFailed(row.id);
      }
      getState().upsertApplication(row);
      // The build session was created server-side, so the sidebar list
      // doesn't have it yet — pull it in so it's listed under its agent and
      // the chat header can name it when the user opens it.
      if (row.session_id) void hydrateSession(row.session_id);
      break;
    }

    // Archived rows leave every open client's list, exactly like deleted ones;
    // the Applications page fetches the archive itself.
    case "application_archived":
    case "application_deleted": {
      const id =
        (data.application_id as string) ||
        (data.application as Application | undefined)?.id;
      if (id) getState().removeApplication(id);
      break;
    }

    // Agents (sidebar-pins.md §7) — the same global shape as applications:
    // the row comes down whole and the store mirrors it.
    case "agent_created":
    case "agent_updated": {
      const row = data.agent as Agent | undefined;
      if (row) getState().upsertAgent(row);
      break;
    }

    case "agent_archived":
    case "agent_deleted": {
      const id = data.agent_id as string | undefined;
      if (id) getState().removeAgent(id);
      break;
    }

    case "bg_started": {
      const { upsertBgTask } = getState();
      upsertBgTask(sessionId, {
        id: data.task_id as string,
        session_id: sessionId,
        command: data.command as string,
        description: (data.description as string) ?? null,
        working_dir: "",
        status: "running",
        exit_code: null,
        stdout: "",
        stderr: "",
        truncated: false,
        started_at: data.started_at as string,
        completed_at: null,
        idle_timeout_seconds: (data.idle_timeout_seconds as number | null) ?? null,
        timeout_seconds: (data.timeout_seconds as number | null) ?? null,
        stop_reason: null,
      });
      break;
    }

    case "bg_completed": {
      // Patch the existing row in place. We don't have stdout/stderr
      // here — those land via the synthesized user_message turn — but
      // the chip needs the status + exit_code to flip from spinner
      // to badge. Full bytes are fetched on demand via the REST GET.
      const { bgTasks, upsertBgTask } = getState();
      const current = (bgTasks[sessionId] || []).find(
        (t) => t.id === data.task_id
      );
      if (current) {
        upsertBgTask(sessionId, {
          ...current,
          status: data.status as BgTaskStatus,
          exit_code: (data.exit_code as number | null) ?? null,
          truncated: !!data.truncated,
          completed_at: data.completed_at as string,
          idle_timeout_seconds: (data.idle_timeout_seconds as number | null)
            ?? current.idle_timeout_seconds ?? null,
          timeout_seconds: (data.timeout_seconds as number | null)
            ?? current.timeout_seconds ?? null,
          stop_reason: (data.stop_reason as string | null) ?? null,
        });
      }
      break;
    }

    case "research_started": {
      getState().upsertResearch(sessionId, {
        id: data.research_id as string,
        session_id: sessionId,
        question: data.question as string,
        status: "running",
        phase: "scope",
      });
      break;
    }

    case "research_progress": {
      getState().upsertResearch(sessionId, {
        id: data.research_id as string,
        phase: (data.phase as string) ?? null,
        detail: data.detail as string,
        counts: (data.counts as Record<string, number>) ?? undefined,
      });
      break;
    }

    case "research_completed": {
      getState().upsertResearch(sessionId, {
        id: data.research_id as string,
        status: "completed",
        phase: "done",
        sources: (data.sources as string[]) ?? undefined,
        verified: (data.verified as number) ?? undefined,
        unverified: (data.unverified as number) ?? undefined,
        injection_status: (data.injection_status as ResearchJob["injection_status"]) ?? "pending",
      });
      break;
    }

    case "research_delivery": {
      getState().upsertResearch(sessionId, {
        id: data.research_id as string,
        injection_status: data.injection_status as ResearchJob["injection_status"],
        error: (data.error as string) ?? null,
        ...(data.delivery_receipt ? { delivery_receipt: data.delivery_receipt as ResearchJob["delivery_receipt"] } : {}),
      });
      break;
    }

    case "research_failed": {
      getState().upsertResearch(sessionId, {
        id: data.research_id as string,
        status: (data.status as ResearchJob["status"]) ?? "failed",
        error: (data.error as string) ?? null,
      });
      break;
    }

    case "session_archived": {
      // Another client (or this tab's archive POST) hid the old
      // session and replaced it with new_session_id. Update the
      // sessions list + swap active if this tab was on the old one.
      const oldId = data.old_session_id as string;
      // null = the session was hidden with no replacement (a scheduler-origin
      // session auto-archiving on idle, or an agent being archived).
      const newId = data.new_session_id as string | null;
      const name = data.name as string;
      const settings = {
        model: (data.model as SessionInfo["model"]) ?? null,
        reasoning_effort: (data.reasoning_effort as SessionInfo["reasoning_effort"]) ?? null,
        service_tier: (data.service_tier as SessionInfo["service_tier"]) ?? null,
      };
      const store = getState();
      const prev = store.sessions.find((s) => s.id === oldId);
      const next = store.sessions.filter((s) => s.id !== oldId).map((s) =>
        s.id === newId ? { ...s, ...settings } : s
      );
      if (newId && !next.some((s) => s.id === newId)) {
        next.push({
          id: newId,
          name,
          working_dir: prev?.working_dir ?? "",
          status: "idle",
          created_at: new Date().toISOString(),
          message_count: 0,
          transcript_integrity: "ok",
          claude_session_id: null,
          credential_id: prev?.credential_id ?? null,
          agent_id: prev?.agent_id ?? null,
          origin: prev?.origin ?? "user",
          backend: prev?.backend ?? "claude-code",
          ...settings,
          can_fork: prev?.can_fork ?? true,
          // Carried from the session it replaces: same agent, same backend,
          // so the same harness capability.
          can_steer: prev?.can_steer ?? false,
          fork_is_full_copy: false,
          archived: false,
        });
      }
      store.setSessions(next);
      // Keep the archived session resolvable (so e.g. a fork's now-archived
      // parent renders its name in the fork banner instead of "(deleted
      // session)", and its "open parent" link works).
      if (prev) {
        const restArchived = store.archivedSessions.filter((s) => s.id !== oldId);
        store.setArchivedSessions([{ ...prev, archived: true }, ...restArchived]);
      }
      if (store.activeSessionId === oldId) {
        store.setActiveSessionId(newId);
        if (newId) {
          store.setMessages(newId, []);
          store.setPendingQuestions(newId, []);
          void loadSessionSnapshot(newId, store).catch(() => {});
        }
      }
      break;
    }
    case "session_forked": {
      // A /fork duplicate was created (session-fork.md). The parent is
      // untouched, so we only ADD the new session to the list. The initiating
      // tab already added + switched to it via handleDuplicated; dedupe by id
      // so this broadcast is a no-op there and a plain add elsewhere. We don't
      // switch the active session — only the initiator follows the fork.
      const forkId = data.fork_session_id as string;
      const store = getState();
      if (store.sessions.some((s) => s.id === forkId)) break;
      // Fetch the authoritative SessionInfo rather than synthesizing a partial
      // one (Vera review) — gives the sidebar the right working_dir, backend,
      // fork flags, etc. Re-dedupe inside the callback in case the initiating
      // tab's own add raced in first.
      const tok = store.token;
      if (!tok) break;
      void fetch(`${window.location.origin}/api/sessions/${forkId}`, {
        headers: { Authorization: `Bearer ${tok}` },
      })
        .then((r) => (r.ok ? r.json() : null))
        .then((detail) => {
          if (detail) addSessionInfo(sessionInfoFromDetail(detail));
        })
        .catch(() => {});
      break;
    }
  }
}

export function useWebSocket() {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const token = useSessionStore((s) => s.token);
  const submissions = useRef(new Map<string, {
    sessionId: string;
    finish: (reply: SubmissionReply) => void;
  }>());
  const controls = useRef(new Map<string, { sessionId: string; operation: string; finish: (reply: SubmissionReply) => void }>());

  useEffect(() => {
    if (!token) return;
    const accountGeneration = getState().accountGeneration;
    const pendingSubmissions = submissions.current;
    const pendingControls = controls.current;

    function connect() {
      if (wsRef.current?.readyState === WebSocket.OPEN ||
          wsRef.current?.readyState === WebSocket.CONNECTING) return;

      const ws = new WebSocket(`${WS_URL}?token=${encodeURIComponent(token)}`);
      wsRef.current = ws;

      ws.onopen = () => {
        if (wsRef.current !== ws || getState().accountGeneration !== accountGeneration || getState().token !== token) return;
        getState().setConnected(true);
        if (reconnectTimer.current) clearTimeout(reconnectTimer.current);

        // Re-fetch state after reconnect
        const snapshotStart = getState();
        const { activeSessionId, token: t } = snapshotStart;
        if (t) {
          fetch(`${window.location.origin}/api/sessions`, {
            headers: { Authorization: `Bearer ${t}` },
          })
            .then((r) => (r.ok ? r.json() : null))
            .then((sessions) => sessions && getState().setSessions(sessions))
            .catch(() => {});

          // Applications: a build that finished while we were disconnected
          // would otherwise leave the sidebar showing "building" forever.
          fetch(`${window.location.origin}/api/applications`, {
            headers: { Authorization: `Bearer ${t}` },
          })
            .then((r) => (r.ok ? r.json() : null))
            .then((apps) => apps && getState().setApplications(apps))
            .catch(() => {});

          if (activeSessionId) {
            loadSessionSnapshot(activeSessionId, snapshotStart).catch(() => {});
            // Bg tasks: same reload, independent endpoint. Chat history
            // contains bg_run tool_use blocks whose chips need the bg
            // task records to render correctly after a reconnect.
            loadBgTaskSnapshot(activeSessionId, snapshotStart).catch(() => {});
            fetch(
              `${window.location.origin}/api/sessions/${activeSessionId}/delegations`,
              { headers: { Authorization: `Bearer ${t}` } }
            )
              .then((r) => (r.ok ? r.json() : null))
              .then((delegations) => {
                if (delegations && wsRef.current === ws) {
                  getState().setDelegations(activeSessionId, delegations);
                }
              })
              .catch(() => {});
            // Research jobs: same reconnect reload, so an in-flight job's card
            // (and recently-finished ones) reappear without a missed
            // `research_started` (native-deep-research.md §7).
            fetch(
              `${window.location.origin}/api/sessions/${activeSessionId}/research`,
              { headers: { Authorization: `Bearer ${t}` } }
            )
              .then((r) => (r.ok ? r.json() : null))
              .then((jobs) => {
                if (jobs) getState().setResearch(activeSessionId, jobs, snapshotStart.research[activeSessionId] || []);
              })
              .catch(() => {});
          }
        }
      };

      ws.onclose = (event) => {
        if (wsRef.current !== ws || getState().accountGeneration !== accountGeneration || getState().token !== token) return;
        for (const pending of submissions.current.values()) {
          pending.finish({ accepted: false, state: "unknown", message: "Admission was not confirmed. Retry confirmation with the same submission identity." });
        }
        submissions.current.clear();
        for (const pending of controls.current.values()) {
          pending.finish({ accepted: false, state: "unknown", message: "Command delivery is unknown after disconnect. Refresh the conversation before trying again." });
        }
        controls.current.clear();
        getState().setConnected(false);
        // A revoked or disabled account must authenticate again; reconnecting
        // with the same rejected bearer cannot recover this connection.
        if (event.code === 4001) {
          getState().setToken("");
          return;
        }
        reconnectTimer.current = setTimeout(connect, 3000);
      };

      ws.onerror = () => {
        ws.close();
      };

      ws.onmessage = (e) => {
        if (wsRef.current !== ws || getState().accountGeneration !== accountGeneration || getState().token !== token) return;
        try {
          const data = JSON.parse(e.data);
          if (data.type === "submission") {
            const pending = submissions.current.get(data.client_request_id);
            if (pending && pending.sessionId === data.session_id) {
              submissions.current.delete(data.client_request_id);
              pending.finish({ accepted: data.accepted === true, message: data.message,
                ...(data.state === "unknown" ? { state: "unknown" as const } : {}),
                ...(typeof data.code === "string" ? { code: data.code } : {}),
                ...(typeof data.request_id === "string" ? { requestId: data.request_id } : {}),
                ...(data.execution ? { execution: data.execution } : {}),
              });
            }
          } else if (data.type === "command_receipt") {
            const pending = controls.current.get(data.client_command_id);
            if (pending && pending.sessionId === data.session_id && pending.operation === data.operation) {
              controls.current.delete(data.client_command_id);
              pending.finish({ accepted: data.accepted === true, message: data.message, code: data.code });
            }
          } else {
            handleWsMessage(data);
          }
        } catch {
          // ignore
        }
      };
    }

    connect();

    return () => {
      for (const pending of pendingSubmissions.values()) {
        pending.finish({ accepted: false, state: "unknown", message: "Admission was not confirmed. Retry confirmation with the same submission identity." });
      }
      pendingSubmissions.clear();
      for (const pending of pendingControls.values()) {
        pending.finish({ accepted: false, state: "unknown", message: "Command delivery was not confirmed. Refresh the conversation before trying again." });
      }
      pendingControls.clear();
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      reconnectTimer.current = undefined;
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [token]);

  const send = useCallback((data: Record<string, unknown>) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data));
      return true;
    }
    return false;
  }, []);

  const sendMessage = useCallback(
    (sessionId: string, content: string, attachmentIds?: string[], wikiMode?: WikiMessageMode, submissionId?: string, recoveryOfClientId?: string): Promise<SubmissionReply> => {
      // Don't optimistically add to chat — the backend broadcasts a
      // user_message event whether the prompt fires immediately or after
      // dequeue, so a single broadcast handler keeps state consistent.
      const payload: Record<string, unknown> = {
        type: "send_message",
        session_id: sessionId,
        content,
      };
      if (attachmentIds && attachmentIds.length > 0) {
        payload.attachment_ids = attachmentIds;
      }
      if (wikiMode) payload.wiki_mode = wikiMode;
      if (recoveryOfClientId) payload.recovery_of_client_id = recoveryOfClientId;
      let clientRequestId: string;
      try {
        clientRequestId = submissionId || newSubmissionId();
      } catch (error) {
        return Promise.resolve({ accepted: false, code: "secure_randomness_unavailable",
          message: error instanceof Error ? error.message : "Secure randomness is unavailable." });
      }
      payload.client_request_id = clientRequestId;
      if (wsRef.current?.readyState !== WebSocket.OPEN) {
        return Promise.resolve({ accepted: false, code: "not_sent", message: "Disconnected. Your message was not sent." });
      }
      return new Promise<SubmissionReply>((finish) => {
        const timeout = setTimeout(() => {
          submissions.current.delete(clientRequestId);
          finish({ accepted: false, state: "unknown", clientRequestId, message: "Admission was not confirmed. Retry confirmation with the same submission identity." });
        }, 30_000);
        submissions.current.set(clientRequestId, { sessionId, finish: (reply) => {
          clearTimeout(timeout);
          finish({ ...reply, clientRequestId });
        } });
        try {
          if (!send(payload)) throw new Error("Disconnected");
        } catch {
          clearTimeout(timeout);
          submissions.current.delete(clientRequestId);
          finish({ accepted: false, code: "not_sent", message: "Your message could not be sent." });
        }
      });
    },
    [send]
  );

  const sendCommand = useCallback((data: Record<string, unknown>): Promise<SubmissionReply> => {
    const sessionId = String(data.session_id);
    const operation = String(data.type);
    let id: string;
    try {
      id = newSubmissionId();
    } catch (error) {
      const message = error instanceof Error ? error.message : "Secure randomness is unavailable.";
      // A local failed notice has no wire identity and cannot be replayed.
      getState().setControlCommand({ id: `unsent:${sessionId}:${operation}`, sessionId, operation, state: "failed", message });
      return Promise.resolve({ accepted: false, code: "secure_randomness_unavailable", message });
    }
    const generation = getState().accountGeneration;
    const update = (state: "pending" | "accepted" | "failed" | "unknown", message?: string) => {
      if (getState().accountGeneration !== generation) return;
      if (operation === "interrupt" && state === "accepted") {
        getState().dismissControlCommand(id);
      } else {
        getState().setControlCommand({ id, sessionId, operation, state, message });
      }
    };
    update("pending");
    return new Promise<SubmissionReply>((resolve) => {
      const timeout = setTimeout(() => {
        controls.current.delete(id);
        finish({ accepted: false, state: "unknown", message: "Command confirmation timed out. Refresh before trying again." });
      }, 15_000);
      const finish = (reply: SubmissionReply) => {
        clearTimeout(timeout);
        update(reply.state === "unknown" ? "unknown" : reply.accepted ? "accepted" : "failed", reply.message);
        resolve(reply);
      };
      controls.current.set(id, { sessionId, operation, finish });
      try {
        if (!send({ ...data, client_command_id: id })) {
          controls.current.delete(id);
          finish({ accepted: false, message: "Disconnected. The command was not sent." });
        }
      } catch {
        controls.current.delete(id);
        finish({ accepted: false, message: "The command could not be sent." });
      }
    });
  }, [send]);

  const interrupt = useCallback(
    (sessionId: string) => {
      const state = getState();
      const requestId = state.dispatchStates[sessionId]?.active_request_id ?? null;
      const active = state.activeAttempts[sessionId];
      return sendCommand({ type: "interrupt", session_id: sessionId, request_id: requestId,
        attempt_id: active?.request_id === requestId ? active.attempt_id : null,
        generation: state.dispatchStates[sessionId]?.generation ?? null,
        revision: state.dispatchStates[sessionId]?.revision ?? null });
    },
    [sendCommand]
  );

  const answerQuestion = useCallback(
    (
      sessionId: string,
      questionId: string,
      answers: { selected?: string[]; text?: string }[]
    ) => {
      const question = getState().pendingQuestions[sessionId]?.find((q) => q.question_id === questionId);
      return sendCommand({
        type: "answer_question",
        session_id: sessionId,
        question_id: questionId,
        answers,
        request_id: question?.request_id ?? null, attempt_id: question?.attempt_id ?? null,
      });
    },
    [sendCommand]
  );

  return { send, sendMessage, interrupt, answerQuestion };
}
