import { useMemo, useState } from "react";
import {
  IconChevronDown,
  IconChevronRight,
  IconTool,
  IconFile,
  IconArrowBarToRight,
  IconGitFork,
  IconRobot,
  IconHelpCircle,
} from "@tabler/icons-react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import {
  useSessionStore,
  type AttachmentMetadata,
  type Message,
} from "../stores/sessionStore";
import { BgTaskChip } from "./BgTaskChip";
import {
  AgentDelegationEventCard,
  parseDelegationEvent,
} from "./AgentDelegationEventCard";
import { AgentDelegationRequestCard } from "./AgentDelegationRequestCard";
import { NativeSubagentCard, SubagentCard } from "./SubagentCard";
import { projectToolCall, readRuntimeMetadata } from "../lib/toolIdentity";
import { questionClosure } from "../lib/questionPresentation";
import { requestSource } from "../lib/injectedTurns";
import { MessageDetails, SavedMessageRecord } from "./MessageDetails";
import { ToolInputDetails } from "./ToolInputDetails";

// Marker the backend prepends to the synthesized user message it
// injects when a bg task completes. Used to render those messages
// with a distinct "auto" badge so the user knows they didn't type it.
// Source of truth: server/bg_tasks.py render_delivery_prompt.
const BG_TASK_RESULT_PREFIX = "[bg-task-result]";

import { normalizeMessage, presentOutcome } from "../lib/turnOutcome";

function hasAsyncQuestions(value: unknown): boolean {
  if (!value || typeof value !== "object" || !("type" in value) || value.type !== "agentMessage" ||
      !("questions" in value) || !Array.isArray(value.questions) || !value.questions.length) return false;
  return value.questions.every((question: unknown) => question !== null && typeof question === "object" &&
    "title" in question && typeof question.title === "string" && question.title.trim().length > 0);
}

interface MessageBubbleProps {
  message: Message;
  sessionId: string;
  // Name + avatar of the agent that owns this session, used to label
  // assistant turns. Falls back to a harness-neutral "Assistant" when the
  // session has no owning agent.
  agentName?: string;
  agentAvatar?: string | null;
  // Render the body as plain text instead of markdown. Used for text that is
  // still streaming (inline-steering.md §12): re-parsing a growing string
  // through remark-gfm + remark-math on every 50ms flush is O(n²) work on a
  // long answer and janks the main thread, and half-written markdown (an
  // unclosed code fence, a lone table row) renders broken while it arrives.
  // The completed block that replaces it renders as markdown normally.
  plain?: boolean;
  // "Fork from here" affordance (session-rewind.md §6.1): rendered on
  // user messages when set + the message has a known seq. Called with the
  // rewind target seq.
  onFork?: (seq: number) => void;
  onRestoreInput?: (message: Message) => void;
  restoreInputDisabled?: boolean;
}

function OutcomeResult({ message, sessionId, onRestoreInput, restoreInputDisabled }: Pick<MessageBubbleProps, "message" | "sessionId" | "onRestoreInput" | "restoreInputDisabled">) {
  const transcript = useSessionStore((state) => state.messages[sessionId]);
  const { outcome, label, primary, recoveries, outputSaved, recordingNote } = presentOutcome(message, transcript);
  return <div className="msg msg-system flex flex-col items-center gap-1 py-1">
    <span className={`result-badge rounded-full px-3 py-1 font-mono text-[11px] uppercase tracking-wider ${
      outcome.status === "failed" ? "bg-danger-bg text-destructive" : "bg-gray-100 text-gray-700"
    }`}>
      {label}{message.cost != null ? ` · $${message.cost.toFixed(4)}` : ""}
    </span>
    {primary && <div className="max-w-[85%] whitespace-pre-wrap break-words text-sm">{primary}</div>}
    {(outcome.native_status || outcome.compact?.native_command || recordingNote || recoveries > 0 || outputSaved) &&
      <details className="max-w-[85%] text-xs text-muted-foreground">
        <summary className="cursor-pointer">Completion details</summary>
        {recoveries > 0 && <p>{recoveries} {recoveries === 1 ? "recovery" : "recoveries"}</p>}
        {outcome.native_status && <p>Native: {outcome.native_status}</p>}
        {outcome.compact?.native_command && <pre aria-label="Native command details" className="whitespace-pre-wrap break-all">
          {JSON.stringify(outcome.compact.native_command, null, 2)}
        </pre>}
        {outputSaved && <p>Model output was saved.</p>}
        {recordingNote && <p>{recordingNote}</p>}
        {outcome.recording_issues && Object.keys(outcome.recording_issues).length > 0 &&
          <pre className="whitespace-pre-wrap break-all">{JSON.stringify(outcome.recording_issues, null, 2)}</pre>}
        {outcome.recording_locations?.length ? <pre className="whitespace-pre-wrap break-all">{JSON.stringify(outcome.recording_locations, null, 2)}</pre> : null}
      </details>}
    {outcome.code === "attachment_unavailable" && Boolean(message.tool_input?.rejected_input) && onRestoreInput &&
      <button type="button" className="text-sm text-primary underline" disabled={restoreInputDisabled}
        onClick={() => onRestoreInput(message)}>Restore draft and re-upload files</button>}
  </div>;
}

function RecoveryMessage({ message }: { message: Message }) {
  const recovery = normalizeMessage(message).recovery!;
  return <details className="msg msg-recovery rounded-lg border border-border px-3 py-2 text-xs text-muted-foreground">
    <summary className="cursor-pointer">
      {recovery.phase === "retrying" ? "Recovering" : recovery.phase === "recovered" ? "Recovered" : "Backend notification"}
      {recovery.attempt != null && recovery.maximum != null ? ` · attempt ${recovery.attempt}/${recovery.maximum}` : ""}
    </summary>
    <div className="mt-2 whitespace-pre-wrap break-words">{recovery.message}</div>
  </details>;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function AttachmentList({
  attachments,
  sessionId,
}: {
  attachments: AttachmentMetadata[];
  sessionId: string;
}) {
  const token = useSessionStore((s) => s.token);
  return (
    <div className="msg-attachments mt-2 flex flex-wrap gap-2">
      {attachments.map((a) => {
        // Token is appended as a query param because <img src> and
        // <a download> can't carry custom headers. Same auth value as the
        // bearer header — the server accepts either.
        const url = `${window.location.origin}/api/sessions/${encodeURIComponent(
          sessionId
        )}/attachments/${encodeURIComponent(a.id)}?token=${encodeURIComponent(token)}`;
        const isImage = a.mime_type.startsWith("image/");
        if (isImage) {
          return (
            <a
              key={a.id}
              href={url}
              target="_blank"
              rel="noreferrer"
              className="attachment-thumb block rounded-md border border-border overflow-hidden bg-muted/40 hover:border-primary/60 transition-colors"
              title={`${a.filename} (${formatBytes(a.size)})`}
            >
              <img
                src={url}
                alt={a.filename}
                className="block max-h-40 max-w-[16rem] object-contain"
              />
            </a>
          );
        }
        return (
          <a
            key={a.id}
            href={url}
            download={a.filename}
            target="_blank"
            rel="noreferrer"
            className="attachment-chip inline-flex items-center gap-2 rounded-md border border-border bg-muted/40 px-2.5 py-1.5 text-xs text-foreground hover:border-primary/60 transition-colors"
          >
            <IconFile size={14} className="text-muted-foreground shrink-0" />
            <span className="font-medium truncate max-w-[12rem]">{a.filename}</span>
            <span className="text-muted-foreground">{formatBytes(a.size)}</span>
          </a>
        );
      })}
    </div>
  );
}

export function MessageBubble(props: MessageBubbleProps) {
  return <MessageDetails key={`${props.sessionId}:${props.message.seq}:${props.message.detail_ref}`}
    sessionId={props.sessionId} message={props.message}
    expandWithMessage={props.message.type === "tool_use" || props.message.type === "tool_result"}>{(message, loadDetail) =>
      message !== props.message && (message.type === "native_subagent" || props.message.identity_unavailable)
        ? <SavedMessageRecord message={message} label={props.message.identity_unavailable ? "Full saved message record" : "Full saved helper record"} />
        : <MessageBody {...props} message={normalizeMessage(message)} onExpand={loadDetail} />
    }</MessageDetails>;
}

function MessageBody({
  message,
  sessionId,
  agentName,
  agentAvatar,
  plain = false,
  onFork,
  onRestoreInput,
  restoreInputDisabled,
  onExpand,
}: MessageBubbleProps & { onExpand?: () => void }) {
  const assistantLabel = agentName || "Assistant";
  // Mirror the header's avatar treatment: show the agent's emoji, falling
  // back to the default octopus when an agent exists but set no avatar.
  const assistantAvatar = agentName ? agentAvatar || "🐙" : null;
  const asyncQuestion = !plain && message.role === "assistant" &&
    (message.tool_input?.async_questions_preview === true || hasAsyncQuestions(message.tool_input?.native_item));
  switch (message.type) {
    case "native_subagent":
      return typeof message.tool_input?.card_id === "string"
        ? <NativeSubagentCard sessionId={sessionId} cardId={message.tool_input.card_id} /> : null;
    case "text":
      if (message.role === "user") {
        const source = requestSource(message);
        const isBgResult = source === "bg" || (source === undefined &&
          typeof message.content === "string" &&
          message.content.startsWith(BG_TASK_RESULT_PREFIX));
        if (isBgResult) {
          return (
            <BgTaskResultMessage
              content={message.content as string}
              sessionId={sessionId}
            />
          );
        }
        if (source === "wiki" && typeof message.content === "string" &&
            message.content.startsWith("[wiki-maintenance-result]")) {
          return <div className="msg msg-system py-1 text-center text-xs text-muted-foreground">
            Wiki maintenance results received
          </div>;
        }
        // Agent-to-agent delegation injection (reply / question /
        // error). The DelegationManager injects these as user-message
        // turns starting with a `[agent-…:<name> delegation=…]` prefix.
        const delegationEvent = (source === undefined || source === "delegation") && parseDelegationEvent(
          typeof message.content === "string" ? message.content : undefined
        );
        if (delegationEvent) {
          return <AgentDelegationEventCard event={delegationEvent} />;
        }
        return (
          /* No "You" label: the design says who spoke with the bubble's side
             and fill, and a label on every turn doubles the vertical noise in
             a long transcript. Rewind stays, revealed on hover. */
          <div className="msg msg-user group flex justify-end">
            <div className="max-w-[64%] space-y-1">
              {source && source !== "human" && <div className="msg-source-badge text-right text-xs text-muted-foreground">
                Auto · {source}
              </div>}
              {onFork && typeof message.seq === "number" && (source === undefined || source === "human") && (
                /* `pr-12` reserves the corner the hover timestamp occupies —
                   the two used to overlap, with the time drawn over the label. */
                <div className="flex justify-end pr-12">
                  <button
                    type="button"
                    data-testid="fork-from-here"
                    className="fork-from-here inline-flex items-center gap-1 text-[11px] text-gray-700 opacity-0 transition-opacity hover:text-primary group-hover:opacity-100"
                    title="Rewind to this message and redo it"
                    onClick={() => onFork(message.seq as number)}
                  >
                    <IconGitFork size={12} />
                    Rewind to here
                  </button>
                </div>
              )}
              <div className="msg-content inline-block whitespace-pre-wrap break-words rounded-[14px] rounded-br-[4px] bg-primary px-4 py-3 text-[14px] leading-relaxed text-white">
                {message.content}
              </div>
              {(message.steered || message.tool_input?.native_steer === true) && (
                /* Sent INTO the turn that was already running, rather than
                 * queued behind it (inline-steering.md §12). Worth saying so:
                 * the two look identical in the transcript but behave very
                 * differently, and knowing which you got is the whole point of
                 * having pressed enter mid-turn. */
                <div className="msg-steered-marker flex justify-end">
                  <span className="inline-flex items-center gap-1 font-mono text-[10.5px] text-primary-300">
                    <IconArrowBarToRight size={11} />
                    {message.tool_input?.native_steer === true
                      ? "native input · processing unconfirmed" : "sent to the running turn"}
                  </span>
                </div>
              )}
              {message.attachments && message.attachments.length > 0 && (
                <AttachmentList
                  attachments={message.attachments}
                  sessionId={sessionId}
                />
              )}
            </div>
          </div>
        );
      }
      return (
        /* Ordinary replies use plain prose; async questions need a visible
           reply cue while keeping the original text and options together. */
        <div className="msg msg-assistant flex gap-3">
          <span
            className="tile tile-lg tile-plain mt-0.5"
            title={assistantLabel}
            aria-label={assistantLabel}
          >
            {assistantAvatar || "🐙"}
          </span>
          {/* `pr-12`: the hover timestamp sits in the row's top-right corner, so
              the first line of prose stops before it instead of running under it. */}
          <div
            role={asyncQuestion ? "region" : undefined}
            aria-label={asyncQuestion ? "Question for you" : undefined}
            className={`msg-content markdown min-w-0 flex-1 pr-12 text-[14px] leading-[1.65] text-gray-900 ${
              asyncQuestion ? "msg-async-question rounded-lg border-2 border-primary/50 bg-primary-50 p-4" : ""
            }`}
          >
            {asyncQuestion && <>
              <div className="flex items-center gap-2 font-semibold text-primary">
                <IconHelpCircle size={20} aria-hidden />
                Question for you
              </div>
              <p className="text-sm text-muted-foreground">
                Reply in the message box. The agent may continue working while you reply.
              </p>
            </>}
            {plain ? (
              <div className="msg-streaming-text whitespace-pre-wrap break-words">
                {message.content || ""}
              </div>
            ) : (
              <Markdown
                remarkPlugins={[remarkGfm, remarkMath]}
                rehypePlugins={[rehypeKatex]}
              >
                {message.content || ""}
              </Markdown>
            )}
          </div>
        </div>
      );

    case "tool_use":
      return <ToolUseBlock message={message} sessionId={sessionId} onExpand={onExpand} />;

    case "tool_result":
      return <ToolResultBlock message={message} sessionId={sessionId} onExpand={onExpand} />;

    case "tool_approval_request":
      return (
        <div className="msg msg-approval rounded-lg border border-yellow-200 bg-yellow-50 overflow-hidden">
          <div className="px-3 py-2 text-sm">
            <strong>{message.tool_name || "Tool"}</strong>
            <span className="text-muted-foreground"> · Historical tool approval request</span>
          </div>
          <pre className="border-t border-yellow-200 bg-card/60 px-3 py-2 text-xs font-mono overflow-x-auto whitespace-pre-wrap break-words max-h-60 overflow-y-auto">
            {JSON.stringify(message.tool_input, null, 2)}
          </pre>
        </div>
      );

    case "question_request":
    case "question_snoozed":
      return null; // handled by QuestionPrompt

    case "question_closed": {
      const { primary, detail } = questionClosure(message);
      return (
        <div className="msg msg-question-closed px-4 py-2 text-xs text-muted-foreground">
          {primary}
          {detail && detail !== primary && <details><summary>Original closure details</summary><p>{detail}</p></details>}
        </div>
      );
    }

    case "question_answer": {
      const resolution = message.tool_input?.question_resolution as
        { disposition?: string; source?: { kind?: string } } | undefined;
      const source = resolution?.source?.kind;
      const label = resolution?.disposition === "expired" ? "System · expired" : source === "account" ? "You" :
        source === "agent" ? "Agent" : source === "system" ? "System" : "Answer · source unknown";
      return (
        <div className="msg msg-user flex justify-end">
          <div className="max-w-[85%] space-y-1">
            <div className="msg-label text-xs font-semibold text-muted-foreground text-right">
              {label}
            </div>
            <div className="msg-content msg-question-answer inline-block whitespace-pre-wrap break-words rounded-[14px] rounded-br-[4px] bg-primary px-4 py-3 text-[14px] italic leading-relaxed text-white">
              {message.content}
            </div>
          </div>
        </div>
      );
    }

    case "result":
      return <OutcomeResult message={message} sessionId={sessionId} onRestoreInput={onRestoreInput} restoreInputDisabled={restoreInputDisabled} />;

    case "recovery":
      return <RecoveryMessage message={message} />;

    // Client hints and durable notices for retained results share the same
    // presentation, without attributing them to the user or the assistant.
    case "notice":
    case "delivery_notice":
      return (
        <div className="msg msg-notice flex justify-center py-1">
          <span
            className={`notice-pill max-w-[85%] rounded-full border px-3 py-1 text-xs ${
              message.is_error
                ? "border-destructive/30 bg-destructive/10 text-destructive"
                : "border-border bg-muted text-muted-foreground"
            }`}
          >
            {message.content}
          </span>
        </div>
      );

    case "error":
      return (
        <div className="msg msg-error space-y-1">
          <div className="msg-label text-xs font-semibold text-destructive">
            Error
          </div>
          <div className="msg-content whitespace-pre-wrap break-words rounded-xl border border-danger-border bg-danger-bg px-4 py-3 text-[13.5px] text-destructive">
            {message.content}
          </div>
        </div>
      );

    default:
      return null;
  }
}

function ToolUseBlock({
  message,
  sessionId,
  onExpand,
}: {
  message: Message;
  sessionId: string;
  onExpand?: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const preview = (() => {
    const input = message.tool_input;
    if (input && "command" in input) return String(input.command).slice(0, 80);
    if (input && "file_path" in input) return String(input.file_path);
    return null;
  })();

  const isBgRun = message.tool_name === "mcp__bg__run";
  // Agent-to-agent delegation kick-off (agent-collaboration.md §6).
  // The MCP tool's full name is `mcp__<config-key>__<tool-name>`; the
  // ask_agent server is config-key `ask_agent` and exposes its start
  // tool as `ask`, so this matches `mcp__ask_agent__ask`.
  const isAskAgent = message.tool_name === "mcp__ask_agent__ask";
  const askAgentInput = isAskAgent
    ? (message.tool_input as
        | { name?: unknown; delegation_id?: unknown; request?: unknown; files?: unknown }
        | undefined)
    : undefined;
  const askAgentName =
    askAgentInput && typeof askAgentInput.name === "string"
      ? askAgentInput.name
      : "";
  const askAgentRequest =
    askAgentInput && typeof askAgentInput.request === "string"
      ? askAgentInput.request
      : "";
  const askAgentFiles =
    askAgentInput && Array.isArray(askAgentInput.files)
      ? (askAgentInput.files as unknown[]).filter(
          (f): f is string => typeof f === "string"
        )
      : undefined;

  return (
    <div className="space-y-1.5">
      <div className="msg msg-tool overflow-hidden rounded-lg border border-gray-300 bg-gray-50">
        <button
          type="button"
          /* `pr-12`, like every other shape: the hover timestamp lives in
             this corner, and a long tool argument would run under it. */
          className="tool-header flex w-full items-center gap-2 px-3 py-2 pr-12 text-left font-mono text-[12px] transition-colors hover:bg-gray-100"
          onClick={() => { if (!expanded) onExpand?.(); setExpanded(!expanded); }}
        >
          <span className="tool-icon text-muted-foreground shrink-0">
            {expanded ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
          </span>
          <IconTool size={14} className="text-primary shrink-0" />
          <span className="tool-name shrink-0 font-medium text-primary">
            {message.tool_name}
          </span>
          {preview && (
            <code className="tool-preview truncate text-xs text-muted-foreground font-mono">
              {preview}
            </code>
          )}
        </button>
        {expanded && <ToolInputDetails message={message} />}
      </div>
      {isBgRun && <BgChipForToolUse sessionId={sessionId} message={message} />}
      {/* Native sub-agents (native-subagents.md). Deliberately NOT matched on
        * the tool's name: Claude Code has called it `Task` and now `Agent`,
        * and Codex's is `subagent:<tool>`. The run is keyed by tool_call_id,
        * so the card renders exactly when there is a run to show and nothing
        * otherwise — no name list to keep in step with two CLIs. */}
      <SubagentCard sessionId={sessionId} toolCallId={readRuntimeMetadata(message.runtime_metadata)?.tool_call_id} />
      {isAskAgent && askAgentRequest && (
        <AgentDelegationRequestCard
          sessionId={sessionId}
          message={message}
          agentName={askAgentName}
          continued={typeof askAgentInput?.delegation_id === "string"}
          request={askAgentRequest}
          files={askAgentFiles}
        />
      )}
    </div>
  );
}

/** The receipt names the task; the command remains a readable description. */
function BgChipForToolUse({ sessionId, message }: { sessionId: string; message: Message }) {
  const messages = useSessionStore((s) => s.messages[sessionId]);
  const projection = useMemo(() => projectToolCall(message, messages), [message, messages]);
  const taskId = projection.receipt?.kind === "bg" ? projection.receipt.task_id : undefined;
  if (taskId) return <BgTaskChip sessionId={sessionId} taskId={taskId} />;
  return <div className="octo-bgtask-chip inline-flex items-center gap-2 rounded-md border border-border bg-muted/40 px-2.5 py-1.5 text-xs text-muted-foreground">
    <span>{projection.failed ? "Background command failed to start" : projection.result ? "Background task receipt unavailable" : "Waiting for background task receipt…"}</span>
  </div>;
}

function BgTaskResultMessage({
  content,
  sessionId: _sessionId,
}: {
  content: string;
  sessionId: string;
}) {
  const [expanded, setExpanded] = useState(false);
  // Strip the marker for display — it's only a routing hint, not part
  // of the human-facing text.
  const stripped = content.startsWith(BG_TASK_RESULT_PREFIX)
    ? content.slice(BG_TASK_RESULT_PREFIX.length).trimStart()
    : content;
  // Show only the first line collapsed; that line is the "finished
  // with status …" summary which is the most-important info.
  const firstLine = stripped.split("\n", 1)[0];
  return (
    <div className="msg msg-bg-result flex justify-end">
      <div className="max-w-[85%] space-y-1">
        <div className="msg-label text-xs font-semibold text-muted-foreground text-right flex items-center justify-end gap-1.5">
          <IconRobot size={12} className="text-muted-foreground" />
          <span>Auto · bg-task result</span>
        </div>
        <button
          type="button"
          onClick={() => setExpanded((e) => !e)}
          className="msg-content w-full text-left inline-block rounded-lg border border-dashed border-border bg-muted/30 px-4 py-3 text-sm text-foreground hover:bg-muted/50 transition-colors"
          title={expanded ? "Collapse" : "Expand full result"}
          aria-expanded={expanded}
        >
          <div className="flex items-start gap-2">
            <span className="text-muted-foreground shrink-0 mt-0.5">
              {expanded ? <IconChevronDown size={12} /> : <IconChevronRight size={12} />}
            </span>
            {expanded ? (
              <pre className="m-0 flex-1 font-mono text-xs whitespace-pre-wrap break-words">
                {stripped}
              </pre>
            ) : (
              <span className="flex-1">{firstLine}</span>
            )}
          </div>
        </button>
      </div>
    </div>
  );
}

function ToolResultBlock({ message, sessionId, onExpand }: { message: Message; sessionId: string; onExpand?: () => void }) {
  const [expanded, setExpanded] = useState(false);
  const [copyStatus, setCopyStatus] = useState("Copy path");
  const content = message.content || "";
  const previewStr =
    typeof content === "string" ? content.slice(0, 120) : String(content);
  const errored = !!message.is_error;
  const nativeItem = message.runtime_metadata?.native && message.tool_input?.native_item;
  const native = nativeItem && typeof nativeItem === "object" ? nativeItem as Record<string, unknown> : undefined;
  const savedPath = native?.type === "imageGeneration" && native.status === "completed" && !errored
    && typeof native.savedPath === "string" ? native.savedPath : undefined;
  const label = native?.type === "hookPrompt" ? "Hook prompt" : errored ? "Error" : "Result";

  return (
    <div
      className={`msg msg-tool-result rounded-lg border overflow-hidden ${
        errored
          ? "error border-destructive/50 bg-destructive/5"
          : "border-border bg-card"
      }`}
    >
      <button
        type="button"
        /* `pr-12` — the same reserved corner. */
        className="tool-header w-full flex items-center gap-2 px-3 py-2 pr-12 text-left text-sm hover:bg-accent/50 transition-colors"
        onClick={() => { if (!expanded) onExpand?.(); setExpanded(!expanded); }}
      >
        <span className="tool-icon text-muted-foreground shrink-0">
          {expanded ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
        </span>
        <span
          className={`tool-result-label text-xs font-semibold uppercase tracking-wider shrink-0 ${
            errored ? "text-destructive" : "text-green-700"
          }`}
        >
          {label}
        </span>
        {typeof native?.status === "string" && <span className="text-xs text-muted-foreground">{native.status}</span>}
        {!expanded && (
          <code className="tool-preview truncate text-xs text-muted-foreground font-mono">
            {previewStr}
          </code>
        )}
      </button>
      {savedPath && <div className="px-3 pb-2 space-y-1 text-xs">
        <code className="block select-text break-all">{savedPath}</code>
        <div className="flex gap-3">
          <button type="button" className="underline" onClick={() => {
            if (!navigator.clipboard) { setCopyStatus("Copy unavailable"); return; }
            navigator.clipboard.writeText(savedPath).then(() => setCopyStatus("Copied"), () => setCopyStatus("Copy failed"));
          }}>{copyStatus}</button>
          <button type="button" className="underline" onClick={() => useSessionStore.getState().openViewer(sessionId, savedPath)}>
            Open in viewer
          </button>
        </div>
        <p className="text-muted-foreground">If the viewer cannot open this path, open it on the host. The original file may change or disappear.</p>
      </div>}
      {expanded && (
        <pre
          className={`tool-detail border-t px-4 py-2.5 text-xs font-mono whitespace-pre-wrap break-words max-h-80 overflow-y-auto ${
            errored
              ? "border-destructive/30 bg-destructive/5 text-destructive"
              : "border-border bg-muted/40 text-foreground"
          }`}
        >
          {content}
        </pre>
      )}
    </div>
  );
}
