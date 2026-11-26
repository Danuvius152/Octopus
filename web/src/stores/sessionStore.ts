import { create } from "zustand";
import { mergeCardSnapshot } from "../lib/cardSnapshots";
import { confirmResearchDetail, confirmResearchJobSeen, initializeResearchJob, mergeResearchJob, researchChangedSince, researchConfirmedByDetail, researchEventPatch, type ResearchEvent, type ResearchJob } from "../lib/researchProjection";
import { agentOrderIsProtected, agentReadStatusIsCurrent, agentRequestIsCurrent, agentSnapshotCovers, beginAgentRequest, canApplyAgent, canApplyAgentObservation, endAgentRequest, finishAgentOrder, recordAgentChanges, recordAgentOrder, recordAgentOrderReceipt, recordAgentSnapshot, type AgentRequest } from "../lib/agentUpdates";
import { withPinOrder } from "../lib/sidebarPins";
import { unreadList, type ListReadState } from "../lib/listState";
import type { ControlCommandStatus } from "../lib/submission";
import type { QuestionDraft } from "../lib/questionDrafts";
import { normalizeMessage, type MessageInput } from "../lib/turnOutcome";
import { readActiveAttempt, readDispatchState, type ActiveAttempt, type DispatchState } from "../lib/sessionDispatch";
import { nativeItemKey } from "../lib/nativeOutput";
import { readAttemptIdentity, sameAttempt, type AttemptIdentity } from "../lib/toolIdentity";
import { readContextState, type ContextState } from "../lib/sessionContext";
import type { WsEvent } from "../lib/wsEvents";
import type {
  AgentRead as ApiAgentRead,
  ApplicationRead as ApiApplicationRead,
  AttachmentMetadata as ApiAttachmentMetadata,
  BackendCatalog,
  BackendKind as ApiBackendKind,
  BgTaskSummaryResponse as ApiBgTaskSummaryResponse,
  DelegationResponse as ApiDelegationResponse,
  ConnectorCatalogEntry as ApiConnectorCatalogEntry,
  ConnectorInstallationInfo as ApiConnectorInstallationInfo,
  CredentialInfo as ApiCredentialInfo,
  ScheduleInfo,
  SessionInfo as ApiSessionInfo,
  SubagentDefinition as ApiSubagentDefinition,
  SubagentRun as ApiSubagentRun,
  SessionStatus as ApiSessionStatus,
  RuntimeMetadata,
  WikiJobCard,
} from "../api";

export type BackendCatalogRead =
  | { status: "idle" | "loading" | "failed" }
  | { status: "ready"; catalog: BackendCatalog };

export interface FileResourceDirectory {
  path: string;
  path_id: string;
}

export interface FileViewerIntent {
  id: number;
  sessionId: string;
  path: string;
  fragment?: string;
  sourceDirectory?: FileResourceDirectory;
}

let nextFileViewerId = 0;

interface AcceptedTextConsumer { sessionId: string; account: number; notify: () => void }
// Only openings waiting for their one timeout recovery subscribe. The original
// acceptance paths notify without retaining a body or publishing another store.
const acceptedTextConsumers = new Set<AcceptedTextConsumer>();
const NO_TEXT_CONSUMERS: readonly AcceptedTextConsumer[] = [];
export function observeAcceptedText(sessionId: string, account: number, notify: () => void): () => void {
  const consumer = { sessionId, account, notify };
  acceptedTextConsumers.add(consumer);
  return () => { acceptedTextConsumers.delete(consumer); };
}
function textConsumers(sessionId: string, account: number): readonly AcceptedTextConsumer[] {
  if (!acceptedTextConsumers.size) return NO_TEXT_CONSUMERS;
  return [...acceptedTextConsumers].filter((consumer) => consumer.sessionId === sessionId && consumer.account === account);
}
function notifyAcceptedText(consumers: readonly AcceptedTextConsumer[]) {
  for (const consumer of consumers) {
    if (acceptedTextConsumers.has(consumer) && consumer.account === useSessionStore.getState().accountGeneration) consumer.notify();
  }
}

const streamingBytes = (value: unknown) => new TextEncoder().encode(JSON.stringify(value)).length;
// Private facts follow the lifetime of each displayed preview.
const streamingFacts = new WeakMap<{ content: string }, { truncated: boolean; arrival: symbol; afterSeq?: number }>();

export function streamingTranscriptSource(preview: { content: string }) {
  const facts = streamingFacts.get(preview);
  return { text: facts?.truncated ? preview.content.slice(0, -1) : preview.content, afterSeq: facts?.afterSeq };
}

function streamingPreview(previous: { content: string } | undefined, chunk: string, budget: number) {
  const truncated = previous && streamingFacts.get(previous)?.truncated;
  if (previous && (truncated || !chunk)) return { content: previous.content, truncated: truncated ?? false };
  const value = (previous?.content ?? "") + chunk;
  if (streamingBytes(value) <= budget) return { content: value, truncated: false };
  let low = 0, high = Math.min(value.length, budget);
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (streamingBytes(value.slice(0, middle) + "…") <= budget) low = middle;
    else high = middle - 1;
  }
  if (low && /[\uD800-\uDBFF]/.test(value[low - 1])) low--;
  return { content: value.slice(0, low) + "…", truncated: true };
}

export interface StreamingReadBasis {
  root: symbol | undefined;
  native: Record<string, symbol | undefined>;
}

export function captureStreamingRead(sessionId: string): StreamingReadBasis {
  const { streamingText, streamingItems } = useSessionStore.getState();
  const root = streamingText[sessionId];
  return {
    root: root && streamingFacts.get(root)?.arrival,
    native: Object.fromEntries(Object.entries(streamingItems[sessionId] ?? {}).map(([key, item]) =>
      [key, streamingFacts.get(item)?.arrival])),
  };
}
const subagentUpdates = new WeakMap<SubagentRun, symbol>();
const delegationUpdates = new WeakMap<Delegation, symbol>();

// Re-export contract types under the names the rest of the frontend
// already uses. Source of truth is `web/src/api/contracts.ts`, regenerated
// from FastAPI's openapi.json via `bun run generate:contracts`.
export type SessionStatus = ApiSessionStatus;
export type SessionInfo = ApiSessionInfo;
export type SubagentRun = ApiSubagentRun & { display_truncated?: boolean };
export type SubagentDefinition = ApiSubagentDefinition;
export type Agent = ApiAgentRead;
export type Application = ApiApplicationRead;
export type BackendKind = ApiBackendKind;
export type CredentialInfo = ApiCredentialInfo;
export type ConnectorCatalogEntry = ApiConnectorCatalogEntry;
export type ConnectorInstallationInfo = ApiConnectorInstallationInfo;
export type Schedule = ScheduleInfo;
export type AttachmentMetadata = ApiAttachmentMetadata;
export type SessionAuxiliaryKind = "background" | "research";

// `Message` is a UI-only shape: it's how WS events are normalized for
// rendering, not 1-to-1 with `MessageContent` from the contract (which has
// `content: unknown` because tool_result can carry arbitrary JSON). Leave
// hand-rolled.
export interface Message {
  body_state?: "inline" | "preview";
  detail_ref?: string | null;
  identity_unavailable?: boolean;
  native_item_type?: "fileChange" | null;
  role: "user" | "assistant" | "system" | "tool";
  type: string;
  content?: string;
  outcome?: import("../lib/turnOutcome").TurnOutcome;
  recovery?: import("../lib/turnOutcome").TurnRecovery;
  tool_name?: string;
  tool_input?: Record<string, unknown>;
  tool_use_id?: string;
  runtime_metadata?: RuntimeMetadata | null;
  is_error?: boolean;
  session_id?: string;
  cost?: number;
  // User-uploaded files attached to this message. Present on user
  // messages that the user attached files to (image, PDF, anything).
  // The chat UI renders thumbnails / file chips below the message text.
  attachments?: AttachmentMetadata[];
  // True for a message handed to a turn that was already running
  // (inline-steering.md §12) rather than queued behind it. Live-only: on a
  // reload it's an ordinary user message, which is what it is in the
  // transcript — the distinction only matters while you're watching.
  steered?: boolean;
  // Per-session sequence number, present on messages loaded from the
  // session detail snapshot. Used as the rewind target for "Fork from here"
  // (session-rewind.md §6.1). Absent on freshly-streamed messages until
  // the next detail reload.
  seq?: number;
  // When the row was written, ISO-8601. Revealed on hover (`MessageTime`).
  // Absent for messages written before the column existed and for anything
  // not persisted — both render no time rather than a made-up one.
  created_at?: string;
}

export interface StreamingItem {
  content: string;
  runtime_metadata: RuntimeMetadata;
}

interface RootStreamingPreview {
  content: string;
  attempt?: AttemptIdentity;
}

export interface MessagePageBasis {
  window: symbol | undefined;
  beforeSeq: number;
}

export type MessagePageResult = { state: "merged"; added: number } | { state: "obsolete" } |
  { state: "failed"; error: string };

export interface QuestionOption {
  label: string;
  description?: string;
}

export interface QuestionItem {
  question: string;
  header?: string;
  multiSelect?: boolean;
  options?: QuestionOption[] | null;
}

export interface PendingQuestion {
  message_seq?: number | null;
  body_state?: "inline" | "preview";
  detail_ref?: string | null;
  question_id: string;
  questions: QuestionItem[];
  deadline?: string | null;
  request_id?: string | null;
  attempt_id?: string | null;
  interaction_policy?: "human" | "unattended" | null;
  wait_mode?: "blocking" | "optional" | "mcp" | null;
  snoozed?: boolean | null;
}

/** Return `map` without `key`, or `map` itself when the key isn't there.
 * Returning the same object on a miss keeps zustand from waking subscribers
 * for a no-op clear. */
function dropKey<T>(
  map: Record<string, T>,
  key: string
): Record<string, T> {
  if (map[key] === undefined) return map;
  const next = { ...map };
  delete next[key];
  return next;
}

/** The step trail with `description` appended, when it's actually new.
 * Progress repeats the current step while one tool call runs. */
function appendStep(steps: string[] | undefined, description?: string | null) {
  const trail = steps ?? [];
  if (!description || trail[trail.length - 1] === description) return trail;
  return [...trail, description].slice(-40);
}

/** Drop keys whose value is null/undefined/"" so a partial update merges
 * onto the previous one instead of blanking fields it simply didn't
 * restate — the shape every sub-agent progress event arrives in. */
function prune<T extends object>(value: T): Partial<T> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(value)) {
    if (v !== null && v !== undefined && v !== "") out[k] = v;
  }
  return out as Partial<T>;
}

/** What the main pane shows. Octopus has no URL router, so this is the
 * routing state. The manage views (schedules/connectors/harness) are full
 * pages in the main area rather than dialogs, and the create views are
 * full-page forms — both per the console design. */
export type MainView =
  | "chat"
  | "application"
  | "application-create"
  | "agent-form"
  | "schedules"
  | "connectors"
  | "harness"
  | "monitor"
  | "account";

export type PageTab = "all" | "form";

export interface Identity {
  label: string;
  user_id: string | null;
  is_admin: boolean;
}

type DispatchReadStart = Pick<SessionStore, "accountGeneration" | "dispatchGeneration" | "dispatchStates" | "executionObservations">;

interface SessionStore {
  contextStates: Record<string, { generation: string; state: ContextState | null; pending: boolean }>;
  applyContextState: (sessionId: string, generation: string, value: unknown, start?: DispatchReadStart, pending?: boolean) => boolean;
  activeAttempts: Record<string, ActiveAttempt>;
  setActiveAttempt: (sessionId: string, attempt: ActiveAttempt | null) => void;
  // Live execution observations, including absent -> present -> absent.
  // HTTP snapshots use this baseline without claiming a new live observation.
  executionObservations: Record<string, number>;
  applyExecutionSnapshot: (sessionId: string, detail: Record<string, unknown>, start: DispatchReadStart) => boolean;
  controlCommands: Record<string, ControlCommandStatus>;
  setControlCommand: (command: ControlCommandStatus) => void;
  dismissControlCommand: (id: string) => void;
  token: string;
  accountGeneration: number;
  setToken: (t: string) => void;

  /** Who this browser is signed in as (multi-tenancy.md §3), or null before
   * the answer arrives. Held here rather than fetched per component because
   * two of them need it — the account row shows the username, and the
   * sidebar hides the operator-only Monitor page from everybody else. */
  identity: Identity | null;
  setIdentity: (i: Identity | null) => void;

  // Agents own sessions and schedules (agent-refactor.md). The sidebar
  // is two-pane: pick an agent, then see its sessions. `activeAgentId`
  // drives the session/schedule filters.
  agents: Agent[];
  archivedAgents: Agent[];
  archivedAgentsRead: { loaded: boolean; loading: boolean; invalidated: boolean; error: string | null };
  beginAgentRequest: (scope?: AgentRequest["scope"]) => AgentRequest;
  endAgentRequest: (request: AgentRequest) => void;
  applyAgentSnapshot: (agents: Agent[], request: AgentRequest, scope?: AgentRequest["scope"]) => void;
  applyAgentOrder: (ids: string[], request: AgentRequest) => void;
  confirmAgentOrder: (ids: string[], request: AgentRequest) => void;
  finishAgentOrder: (request: AgentRequest) => void;
  upsertAgent: (a: Agent, request?: AgentRequest) => void;
  removeAgent: (id: string, request?: AgentRequest) => void;
  activeAgentId: string | null;
  setActiveAgentId: (id: string | null) => void;

  // Applications (applications.md §7) — agent-built static web apps listed
  // in the sidebar and rendered in the main pane. WS `application_*` events
  // enter ApplicationList, which also owns snapshots and operation receipts.
  applications: Application[];
  archivedApplications: Application[];
  applicationReads: Record<"live" | "archived", ListReadState>;
  applicationPinOrder: string[] | null;
  applicationOrdering: boolean;

  // What the main pane shows. Octopus has no URL router, so this is the
  // routing state: "chat" (the session transcript), "application" (the
  // selected app's iframe), "application-create" (the new-app form).
  // `setActiveSessionId` flips it back to "chat" so every existing call site
  // that selects a session gets the behavior for free.
  mainView: MainView;
  activeApplicationId: string | null;
  // Which agent the agent form is editing; null = the new-agent draft.
  editingAgentId: string | null;
  // Identifies a single opening, including reopening the same Agent in one batch.
  agentFormGeneration: number;
  // Which tab the Agents / Applications page is on (sidebar-pins.md §6):
  // "all" lists every agent or application, "form" creates or edits one.
  // Store state rather than the page's own, so whoever opens the page (the
  // sidebar's "+", the account menu's agent settings) decides the tab.
  pageTab: PageTab;
  setPageTab: (tab: PageTab) => void;
  openApplication: (id: string) => void;
  openApplicationCreate: () => void;
  openAgentForm: (agentId?: string | null) => void;
  // Unpinned applications whose build failed while nobody was looking at
  // them; the sidebar shows them until they're opened (sidebar-pins.md §5).
  unseenFailedApplications: string[];
  noteApplicationFailed: (id: string) => void;
  openManage: (
    view: "schedules" | "connectors" | "harness" | "monitor" | "account"
  ) => void;
  showChat: () => void;

  backendCatalog: BackendCatalogRead;

  sessions: SessionInfo[];
  sessionRead: ListReadState;
  // Explicit, account-local archive relationships; SessionList owns updates.
  sessionSuccessors: Record<string, string>;
  setSessions: (s: SessionInfo[]) => void;
  updateSessionStatus: (id: string, status: SessionStatus) => void;

  // Mirror of `archived=true` rows from `GET /api/sessions?include_archived=true`.
  // SessionList fetches into this lazily when the user expands the
  // archived section; ChatView reads it so it can detect when the
  // active session is archived (show read-only banner, hide input).
  archivedSessions: SessionInfo[];
  setArchivedSessions: (s: SessionInfo[]) => void;

  activeSessionId: string | null;
  setActiveSessionId: (id: string | null) => void;
  forgetSession: (id: string) => void;

  messages: Record<string, Message[]>;
  addMessage: (sessionId: string, msg: MessageInput) => void;
  setMessages: (sessionId: string, msgs: MessageInput[]) => void;
  installTranscript: (sessionId: string, snapshot: {
    messages: MessageInput[]; high: number; oldestSeq: number | null; hasMore: boolean;
  }, streamingBasis?: StreamingReadBasis) => boolean;
  // The transcript arrives windowed to the most recent messages
  // (polish-2026-09.md §4 B2): `oldestSeq` is the lowest seq held for a
  // session and the cursor the next page asks from, `hasMoreMessages` whether
  // there is anything before it. Both come from the snapshot, so a session
  // whose whole transcript fits has `hasMoreMessages: false` and never asks.
  oldestSeq: Record<string, number | null>;
  hasMoreMessages: Record<string, boolean>;
  messageWindows: Record<string, symbol | undefined>;
  // Necessary history read failure, independent of execution/auxiliary reads.
  transcriptReadErrors: Record<string, string>;
  setTranscriptReadError: (sessionId: string, error: string | null, start: { accountGeneration: number }) => void;
  // Accept a page only into the window it extends, in one store update.
  prependMessages: (
    sessionId: string,
    msgs: MessageInput[],
    oldestSeq: number | null,
    hasMore: boolean,
    basis: MessagePageBasis,
  ) => MessagePageResult;

  // Installed snapshot coverage, never advanced by individual WS rows.
  // A live maximum cannot prove that an unseen lower seq was published.
  // The WS reducer also checks identities in the current message window.
  lastAppliedSeq: Record<string, number>;

  schedules: Schedule[];
  scheduleRead: ListReadState;

  credentials: CredentialInfo[];
  credentialRead: ListReadState;

  // Connector catalog and installations (connectors.md). AgentFormPage owns
  // its enablement draft, backed by the agent connector routes.
  connectorCatalog: ConnectorCatalogEntry[];
  connectorCatalogRead: ListReadState;
  connectorInstallations: ConnectorInstallationInfo[];
  connectorRead: ListReadState;

  // Text the model is streaming right now, per session, accumulated from
  // `assistant_delta` frames and dropped the moment the completed block
  // arrives. Never persisted, never part of `messages`.
  streamingText: Record<string, RootStreamingPreview>;
  streamingItems: Record<string, Record<string, StreamingItem>>;
  appendStreamingText: (sessionId: string, chunk: string, metadata?: RuntimeMetadata) => void;
  clearStreamingText: (sessionId: string, attempt?: AttemptIdentity) => void;

  // Sub-agents running inside the current turn, per session, keyed by the
  // tool call that spawned them (native-subagents.md §4). Live UI state, like
  // `streamingText`: the durable record is the Task / collab tool call in
  // `messages`. Kept after completion so the card can show the summary, and
  // re-seeded from the session snapshot on reconnect.
  subagents: Record<string, Record<string, SubagentRun>>;
  upsertSubagent: (sessionId: string, run: SubagentRun) => void;
  setSubagents: (sessionId: string, runs: SubagentRun[], baseline?: Record<string, SubagentRun>, advanceOnly?: boolean) => void;

  dispatchStates: Record<string, DispatchState>;
  dispatchUnavailable: Record<string, boolean>;
  setDispatchUnavailable: (sessionId: string, unavailable: boolean,
    start?: DispatchReadStart) => void;
  wikiJobs: Record<string, WikiJobCard[]>;
  wikiJobReadErrors: Record<string, string>;
  wikiJobDirty: Record<string, Record<string, Pick<WikiJobCard, "job_id" | "vault_id" | "state_version">>>;
  invalidateWikiJob: (sessionId: string, target: Pick<WikiJobCard, "job_id" | "vault_id" | "state_version">) => void;
  mergeWikiJobs: (sessionId: string, cards: WikiJobCard[]) => void;
  dispatchGeneration: string | null;
  retiredDispatchGenerations: string[];
  sessionMembership: Record<string, { generation: string; revision: number }>;
  applyDispatchState: (sessionId: string, state: unknown, start?: DispatchReadStart) => boolean;

  // Deferred /fork requests (session-fork.md). When `/fork` is typed
  // while a session is busy, we record the intent here instead of erroring;
  // a watcher fires the duplicate once the session goes idle + drained.
  // Keyed by the PARENT session id → the requested label (null = default
  // name). Tab-scoped (not persisted) — closing the tab drops it.
  pendingForks: Record<string, { label: string | null }>;
  setPendingFork: (sessionId: string, label: string | null) => void;
  clearPendingFork: (sessionId: string) => void;

  // Active AskUserQuestion prompts waiting for the user's answer.
  pendingQuestions: Record<string, PendingQuestion[]>;
  questionDrafts: Record<string, QuestionDraft>;
  closedQuestionIds: Record<string, Record<string, true>>;
  setPendingQuestions: (sessionId: string, qs: PendingQuestion[]) => void;
  addPendingQuestion: (sessionId: string, q: PendingQuestion) => void;
  removePendingQuestion: (sessionId: string, questionId: string) => void;

  connected: boolean;
  setConnected: (c: boolean) => void;

  // The App-level viewer reads this slot. Each open owns its source
  // session, file path, source directory and fragment until replaced or closed.
  viewer: FileViewerIntent | null;
  openViewer: (sessionId: string, path: string, options?: Pick<FileViewerIntent, "fragment" | "sourceDirectory">) => void;
  closeViewer: () => void;

  // Cross-turn background tasks. Keyed by sessionId → list of tasks
  // (most-recent last as they arrive over WS / from snapshot fetch).
  // The BgTaskChip in chat looks each task up by id; it lives next
  // to the `mcp__bg__run` tool_use block that started it.
  bgTasks: Record<string, BgTask[]>;
  upsertBgTask: (sessionId: string, task: BgTask, baseline?: BgTask | null, advanceOnly?: boolean) => void;
  applyBgTaskEvent: (event: BgTaskEvent) => void;
  setBgTasks: (sessionId: string, tasks: BgTask[], baseline?: BgTask[], claim?: (id: string) => boolean) => void;
  bgTaskReadFailures: Record<string, string[]>;
  researchReadFailures: Record<string, string[]>;
  setAuxiliaryTaskFailure: (kind: SessionAuxiliaryKind, sessionId: string, taskId: string, failed: boolean) => void;
  clearAuxiliaryTaskFailures: (sessionId: string) => void;
  sessionAuxiliaryErrors: Record<string, Partial<Record<SessionAuxiliaryKind, string>>>;
  setSessionAuxiliaryError: (sessionId: string, kind: SessionAuxiliaryKind, error: string | null) => void;

  // Agent-to-agent delegations spawned by a parent session.
  // Keyed by parent sessionId → list of delegation records. The
  // request and event cards in chat resolve their live state by
  // looking up the delegation_id in this map.
  // (agent-collaboration.md §6)
  delegations: Record<string, Delegation[]>;
  setDelegations: (parentSessionId: string, ds: Delegation[], baseline?: Delegation[], advanceOnly?: boolean) => void;
  upsertDelegation: (parentSessionId: string, round: Delegation) => void;

  // Sidebar visibility toggle for `origin === "delegation"` sessions.
  // Hidden by default so heavy fan-out doesn't flood the agent list;
  // toggle persists in localStorage so the user's preference sticks
  // across reloads.
  showDelegations: boolean;
  setShowDelegations: (v: boolean) => void;

  // Whether the sidebar is folded down to its icon rail. Persisted, unlike
  // the per-agent fold state: folding the whole sidebar is a statement about
  // how you want to work ("give the main pane the room"), not a transient
  // navigation step, so it should survive a reload.
  sidebarCollapsed: boolean;
  setSidebarCollapsed: (v: boolean) => void;

  // The mobile drawer. On a phone the sidebar is a slide-over, so *picking*
  // something has to put it away again — leaving it open means every
  // navigation ends with the thing you just chose hidden behind the menu you
  // chose it from. It lives here rather than in App's local state so the
  // close can happen inside the navigation actions themselves, where it
  // can't be forgotten (mobile.md §2).
  sidebarOpen: boolean;
  setSidebarOpen: (v: boolean) => void;

  // Native deep-research jobs, keyed by sessionId → list (native-deep-research.md
  // §7). The ResearchCard renders live phase/progress; the final report arrives
  // as a normal injected turn.
  research: Record<string, ResearchJob[]>;
  initializeResearch: (sessionId: string, job: ResearchJob) => void;
  confirmResearchSeen: (sessionId: string, jobId: string, receipt: NonNullable<ResearchJob["delivery_receipt"]>) => void;
  applyResearchEvent: (event: ResearchEvent) => void;
  applyResearchControl: (sessionId: string, job: ResearchJob, baseline?: ResearchJob) => void;
  applyResearchDetail: (sessionId: string, job: ResearchJob, baseline: ResearchJob | undefined, advanceOnly: boolean) => void;
  setResearch: (sessionId: string, jobs: ResearchJob[], baseline?: ResearchJob[], canReplace?: (id: string) => boolean) => void;
}

export type { ResearchJob } from "../lib/researchProjection";

function updateResearch(s: SessionStore, sessionId: string, id: string, merge: (job?: ResearchJob) => ResearchJob | undefined) {
  const current = s.research[sessionId] ?? [];
  const index = current.findIndex((job) => job.id === id);
  const previous = current[index];
  const updated = merge(previous);
  if (!updated || updated === previous) return s;
  const rows = index < 0 ? [...current, updated] : current.map((job, i) => i === index ? updated : job);
  return { research: { ...s.research, [sessionId]: rows } };
}

// Live events may omit summary fields the card has yet to observe.
type BgOptionalField = "exit_code" | "truncated" | "idle_timeout_seconds" | "timeout_seconds"
  | "stop_reason" | "delivery_disposition" | "delivery_error"
  | "output_preview" | "has_output" | "preview_truncated";
export type BgTask = Omit<ApiBgTaskSummaryResponse, BgOptionalField>
  & Partial<Pick<ApiBgTaskSummaryResponse, BgOptionalField>>;

export function bgTaskPreviewText(text: string, limit: number): string {
  let end = 0;
  for (const character of text) {
    if (limit === 0) return text.slice(0, end);
    end += character.length;
    limit--;
  }
  return text;
}

type BgTaskEvent = Extract<WsEvent, { type: "bg_started" | "bg_completed" | "bg_delivery" }>;
const bgCompletionFields = ["exit_code", "completed_at", "stop_reason", "truncated"] as const;
const bgDeliveryFields = ["delivery_disposition", "delivery_error"] as const;
const bgPreviewFields = ["output_preview", "has_output", "preview_truncated"] as const;
type BgEventField = (typeof bgCompletionFields | typeof bgDeliveryFields)[number];
// Only a WS event carrying a field advances its marker. Snapshots inherit the
// markers so an in-flight read cannot undo a newer observation of that field.
const bgFieldUpdates = new WeakMap<BgTask, Partial<Record<BgEventField, symbol>>>();

type BgTaskSource = { baseline?: BgTask | null; advanceOnly?: boolean } | { event: BgTaskEvent["type"] };

function mergeBgTask(previous: BgTask, patch: Partial<BgTask>, source: BgTaskSource): BgTask {
  const terminal = previous.status !== "running" && previous.status !== "pending";
  const replacesStatus = patch.status !== undefined && patch.status !== previous.status;
  const keepStatus = replacesStatus && (terminal || previous.status === "running" && patch.status === "pending");
  const markers = bgFieldUpdates.get(previous);
  const updated = { ...previous, ...patch };

  if ("event" in source) {
    if (keepStatus) return previous;
    const observed = { ...markers };
    const arrival = Symbol();
    const fields = source.event === "bg_completed" ? bgCompletionFields
      : source.event === "bg_delivery" ? bgDeliveryFields : [];
    for (const field of fields) {
      if (patch[field] !== undefined) observed[field] = arrival;
      else if (source.event === "bg_completed" && !terminal) {
        // Values from the running row provide no evidence about its outcome.
        Object.assign(updated, { [field]: undefined });
      }
    }
    if (source.event === "bg_completed" && !terminal) {
      for (const field of bgPreviewFields) delete updated[field];
    }
    bgFieldUpdates.set(updated, observed);
  } else {
    const basis = source.baseline ? bgFieldUpdates.get(source.baseline) : undefined;
    if (keepStatus) updated.status = previous.status;
    for (const field of bgCompletionFields) {
      if (keepStatus || terminal && previous[field] !== undefined &&
          (source.advanceOnly || markers?.[field] !== basis?.[field])) {
        Object.assign(updated, { [field]: previous[field] });
      }
    }
    for (const field of bgDeliveryFields) {
      if (source.advanceOnly || markers?.[field] !== basis?.[field]) {
        Object.assign(updated, { [field]: previous[field] });
      }
    }
    if (keepStatus || source.advanceOnly && previous.output_preview !== undefined && !replacesStatus) {
      for (const field of bgPreviewFields) {
        Object.assign(updated, { [field]: previous[field] });
      }
    } else if (replacesStatus && isTerminalBgStatus(patch.status) && patch.output_preview === undefined) {
      for (const field of bgPreviewFields) delete updated[field];
    }
    if (markers) bgFieldUpdates.set(updated, markers);
  }
  return updated;
}

function isTerminalBgStatus(status: BgTask["status"] | undefined): boolean {
  return status !== undefined && status !== "running" && status !== "pending";
}

function mergeBgTaskSnapshot(previous: BgTask, task: BgTask, baseline: BgTask | null | undefined, advanceOnly: boolean) {
  if (previous.started_at !== task.started_at) return advanceOnly ? previous : task;
  return mergeBgTask(previous, task, { baseline, advanceOnly });
}

// Live record of an agent-to-agent delegation, mirrored from
// `GET /api/sessions/{parent}/delegations`. Used by the delegation
// request/event cards in chat and by the sidebar inbound badge.
// (agent-collaboration.md §6)
export type Delegation = Omit<ApiDelegationResponse,
  "recording_state" | "work_id" | "terminal_disposition" | "terminal_delivery_error"
> & Partial<Pick<ApiDelegationResponse,
  "work_id" | "terminal_disposition" | "terminal_delivery_error"
>>;

function dispatchUpdate(s: SessionStore, sessionId: string, snapshot: DispatchState, start?: DispatchReadStart): Partial<SessionStore> | null {
  if (start && start.accountGeneration !== s.accountGeneration) return null;
  if (s.retiredDispatchGenerations.includes(snapshot.generation)) return null;
  if (start && start.dispatchGeneration !== s.dispatchGeneration && snapshot.generation !== s.dispatchGeneration) return null;
  const changed = s.dispatchGeneration !== null && s.dispatchGeneration !== snapshot.generation;
  const previous = changed ? undefined : s.dispatchStates[sessionId];
  if (previous && snapshot.revision < previous.revision) return null;
  const errors = { ...(changed ? {} : s.dispatchUnavailable) };
  delete errors[sessionId];
  return {
    dispatchGeneration: snapshot.generation,
    ...(s.dispatchGeneration !== snapshot.generation ? { bgTaskReadFailures: {}, researchReadFailures: {} } : {}),
    ...(changed ? { activeAttempts: {}, executionObservations: {}, closedQuestionIds: {}, pendingQuestions: {},
      sessionMembership: {}, sessionAuxiliaryErrors: {}, streamingText: {}, streamingItems: {}, contextStates: {} } : {}),
    retiredDispatchGenerations: changed ? [...s.retiredDispatchGenerations, s.dispatchGeneration!] : s.retiredDispatchGenerations,
    // Equal revision confirms content and invalidates earlier read failures.
    dispatchStates: { ...(changed ? {} : s.dispatchStates),
      [sessionId]: previous?.revision === snapshot.revision ? { ...previous } : snapshot },
    dispatchUnavailable: errors,
  };
}

function settledQuestions(state: SessionStore, sessionId: string, messages: MessageInput[]): Partial<SessionStore> {
  const ids = messages.filter((message) =>
    (message.type === "question_answer" || message.type === "question_closed") && message.tool_use_id
  ).map((message) => message.tool_use_id!);
  if (!ids.length) return {};
  const closed = { ...state.closedQuestionIds[sessionId], ...Object.fromEntries(ids.map((id) => [id, true as const])) };
  const pending = (state.pendingQuestions[sessionId] || []).filter((question) => !closed[question.question_id]);
  return {
    closedQuestionIds: { ...state.closedQuestionIds, [sessionId]: closed },
    pendingQuestions: pending.length ? { ...state.pendingQuestions, [sessionId]: pending } : dropKey(state.pendingQuestions, sessionId),
  };
}

function hasTerminalFor(messages: MessageInput[], identity: AttemptIdentity | undefined): boolean {
  return Boolean(identity && messages.some((message) =>
    message.type === "result" && sameAttempt(readAttemptIdentity(message), identity)));
}

interface StreamingAdmission {
  terminals: Set<string>;
  collectedItems: Set<string>;
}

// Saved windows are immutable. Admission facts can expire with the array that
// supplied them, independent of which sessions or attempts are still active.
const admissionsByHistory = new WeakMap<Message[], StreamingAdmission>();
const attemptKey = (identity: AttemptIdentity) => JSON.stringify([identity.request_id, identity.attempt_id]);

function streamingAdmission(messages: Message[]): StreamingAdmission {
  const cached = admissionsByHistory.get(messages);
  if (cached) return cached;
  const admission: StreamingAdmission = { terminals: new Set(), collectedItems: new Set() };
  for (const message of messages) {
    if (message.type === "result") {
      const attempt = readAttemptIdentity(message);
      if (attempt) admission.terminals.add(attemptKey(attempt));
    } else if (message.role === "assistant" && message.type === "text") {
      const key = nativeItemKey(message.runtime_metadata);
      if (key) admission.collectedItems.add(key);
    }
  }
  admissionsByHistory.set(messages, admission);
  return admission;
}

function retainStreamingPreviews(state: SessionStore, sessionId: string, keep: (attempt: AttemptIdentity | undefined) => boolean) {
  const root = state.streamingText[sessionId];
  const items = Object.entries(state.streamingItems[sessionId] ?? {});
  const retained = items.filter(([, item]) => keep(item.runtime_metadata));
  return {
    streamingText: root && !keep(root.attempt) ? dropKey(state.streamingText, sessionId) : state.streamingText,
    streamingItems: retained.length === items.length ? state.streamingItems : retained.length
      ? { ...state.streamingItems, [sessionId]: Object.fromEntries(retained) }
      : dropKey(state.streamingItems, sessionId),
  };
}

/** Snapshot coverage also rejects old WS. Installation validity additionally
 * needs cursor metadata, which a raw clear/quarantine withdraws independently. */
export function hasTranscriptBaseline(state: Pick<SessionStore, "lastAppliedSeq" | "oldestSeq" | "hasMoreMessages">, sessionId: string): boolean {
  return state.lastAppliedSeq[sessionId] !== undefined && state.oldestSeq[sessionId] !== undefined &&
    state.hasMoreMessages[sessionId] !== undefined;
}

export function hasEmptyTranscript(
  state: Pick<SessionStore, "lastAppliedSeq" | "oldestSeq" | "hasMoreMessages" | "messages" | "subagents">,
  session: SessionInfo,
): boolean {
  const id = session.id;
  return hasTranscriptBaseline(state, id) && !state.hasMoreMessages[id] && session.status !== "running" &&
    session.transcript_integrity !== "duplicate_seq" && !state.messages[id]?.length &&
    !Object.keys(state.subagents[id] ?? {}).length;
}

function continuousTranscript(previous: Message[], messages: MessageInput[]): boolean {
  const persisted = previous.filter((message) => typeof message.seq === "number");
  const first = messages.find((message) => typeof message.seq === "number")?.seq;
  const seqs = new Set(messages.map((message) => message.seq));
  return first !== undefined && first === persisted[0]?.seq &&
    persisted.every((message) => seqs.has(message.seq));
}

/** Normalized JSON values treat missing optional fields as undefined. */
function sameJsonValue(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (!left || !right || typeof left !== "object" || typeof right !== "object") return false;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left) && Array.isArray(right) && left.length === right.length
      && left.every((value, index) => sameJsonValue(value, right[index]));
  }
  const a = left as Record<string, unknown>, b = right as Record<string, unknown>;
  const keys = Object.keys(a).filter(key => a[key] !== undefined);
  return keys.length === Object.keys(b).filter(key => b[key] !== undefined).length
    && keys.every(key => Object.hasOwn(b, key) && sameJsonValue(a[key], b[key]));
}

function shareTranscript(previous: Message[], messages: MessageInput[]): Message[] {
  const sequenced = new Map(previous.filter(row => typeof row.seq === "number").map(row => [row.seq, row]));
  const occurrences = new Set(previous);
  const rows = messages.map(message => {
    const normalized = normalizeMessage(message);
    // A copied unsequenced notice does not identify the same occurrence,
    // even at the same index. Only its actual retained object can prove that.
    const old = typeof message.seq === "number" ? sequenced.get(message.seq)
      : occurrences.has(message as Message) ? message as Message : undefined;
    return old && sameJsonValue(old, normalized) ? old : normalized;
  });
  return rows.length === previous.length && rows.every((row, index) => row === previous[index]) ? previous : rows;
}

function replaceTranscript(state: SessionStore, sessionId: string, messages: MessageInput[], window: symbol | undefined,
  preserveStreaming: boolean, streamingBasis?: StreamingReadBasis): Partial<SessionStore> {
  const root = state.streamingText[sessionId];
  const keepRoot = root && (preserveStreaming ||
    (streamingBasis && streamingFacts.get(root)?.arrival !== streamingBasis.root)) &&
    !hasTerminalFor(messages, root.attempt);
  const items = state.streamingItems[sessionId];
  const retainedItems = items && (preserveStreaming || streamingBasis) ? Object.entries(items).filter(([key, item]) =>
    (preserveStreaming || streamingFacts.get(item)?.arrival !== streamingBasis?.native[key]) &&
    !hasTerminalFor(messages, item.runtime_metadata) &&
    !messages.some((message) => message.type === "text" && nativeItemKey(message.runtime_metadata) === key)) : [];
  return {
    ...settledQuestions(state, sessionId, messages),
    messageWindows: { ...state.messageWindows, [sessionId]: window },
    messages: { ...state.messages, [sessionId]: shareTranscript(state.messages[sessionId] ?? [], messages) },
    streamingText: keepRoot ? state.streamingText : dropKey(state.streamingText, sessionId),
    streamingItems: retainedItems.length
      ? { ...state.streamingItems, [sessionId]: Object.fromEntries(retainedItems) }
      : dropKey(state.streamingItems, sessionId),
  };
}

function insertMessage(rows: Message[], message: Message): void {
  const before = typeof message.seq === "number" ? rows.findIndex((row) =>
    typeof row.seq === "number" && row.seq > message.seq!) : -1;
  rows.splice(before < 0 ? rows.length : before, 0, message);
}

export const useSessionStore = create<SessionStore>((set, get) => ({
  contextStates: {},
  applyContextState: (sessionId, generation, value, start, pending = false) => {
    const state = readContextState(value);
    if (state === undefined || typeof pending !== "boolean") return false;
    let applied = false;
    set((s) => {
      if (start && start.accountGeneration !== s.accountGeneration || s.retiredDispatchGenerations.includes(generation)
          || s.dispatchGeneration !== null && generation !== s.dispatchGeneration) return s;
      const previous = s.contextStates[sessionId];
      if (previous?.generation === generation) {
        const before = previous.state?.revision ?? 0;
        const incoming = state?.revision ?? 0;
        if (before > incoming || before === incoming && (!pending || previous.pending)) return s;
        if (before === incoming) {
          applied = true;
          return { contextStates: { ...s.contextStates, [sessionId]: { ...previous, pending: true } } };
        }
      }
      applied = true;
      return { contextStates: { ...s.contextStates, [sessionId]: { generation, state, pending } } };
    });
    return applied;
  },
  activeAttempts: {},
  executionObservations: {},
  setActiveAttempt: (sessionId, attempt) => set((s) => ({
    ...(attempt ? retainStreamingPreviews(s, sessionId, (owner) => sameAttempt(attempt, owner)) : {}),
    activeAttempts: attempt ? { ...s.activeAttempts, [sessionId]: attempt } : dropKey(s.activeAttempts, sessionId),
    executionObservations: { ...s.executionObservations, [sessionId]: (s.executionObservations[sessionId] ?? 0) + 1 },
  })),
  applyExecutionSnapshot: (sessionId, detail, start) => {
    const snapshot = readDispatchState(detail.dispatch_state);
    const active = snapshot ? readActiveAttempt(detail.active_attempt, snapshot) : undefined;
    if (!snapshot || active === undefined) {
      const generation = detail.dispatch_state && typeof detail.dispatch_state === "object" && "generation" in detail.dispatch_state
        ? detail.dispatch_state.generation : null;
      if (typeof generation === "string" && get().retiredDispatchGenerations.includes(generation)) return false;
      get().setDispatchUnavailable(sessionId, true, start);
      return false;
    }
    let applied = false;
    set((s) => {
      if (snapshot.generation === s.dispatchGeneration) {
        const observed = start.dispatchGeneration === s.dispatchGeneration ? start.executionObservations[sessionId] ?? 0 : 0;
        if (observed !== (s.executionObservations[sessionId] ?? 0)) return s;
      }
      const patch = dispatchUpdate(s, sessionId, snapshot, start);
      if (!patch) return s;
      if (patch.dispatchStates?.[sessionId].active_request_id !== snapshot.active_request_id) return s;
      const attempts = patch.activeAttempts ?? s.activeAttempts;
      // Detail can see the replacement before attempt_started. Preserve
      // unidentified legacy text and retire confirmed older owners here.
      const previews = active && !patch.streamingText && !sameAttempt(attempts[sessionId], active)
        ? retainStreamingPreviews(s, sessionId, (owner) => !owner || sameAttempt(active, owner)) : {};
      applied = true;
      return { ...patch, ...previews, activeAttempts: active ? { ...attempts, [sessionId]: active } : dropKey(attempts, sessionId) };
    });
    return applied;
  },
  controlCommands: {},
  setControlCommand: (command) => set((s) => ({ controlCommands: { ...s.controlCommands, [command.id]: command } })),
  dismissControlCommand: (id) => set((s) => ({ controlCommands: dropKey(s.controlCommands, id) })),
  identity: null,
  setIdentity: (identity) => {
    const current = get();
    if (identity && current.identity && identity.user_id !== current.identity.user_id) {
      set({ ...useSessionStore.getInitialState(), token: current.token, identity,
        accountGeneration: current.accountGeneration + 1,
        showDelegations: current.showDelegations, sidebarCollapsed: current.sidebarCollapsed });
    } else set({ identity });
  },

  token: localStorage.getItem("octopus_token") || "",
  accountGeneration: 0,
  setToken: (t) => {
    localStorage.setItem("octopus_token", t);
    const current = get();
    if (t === current.token) return;
    set({ ...useSessionStore.getInitialState(), token: t,
      accountGeneration: current.accountGeneration + 1,
      showDelegations: current.showDelegations, sidebarCollapsed: current.sidebarCollapsed,
    });
  },

  agents: [],
  archivedAgents: [],
  archivedAgentsRead: { loaded: false, loading: false, invalidated: false, error: null },
  beginAgentRequest: (scope) => beginAgentRequest(get(), scope),
  endAgentRequest,
  finishAgentOrder,
  applyAgentSnapshot: (rows, request, scope = request.scope) => set((s) => {
    if (!agentRequestIsCurrent(request, s)) return s;
    const incoming = new Map(rows.map((agent) => [agent.id, agent]));
    const current = new Map([...s.agents, ...s.archivedAgents].map((agent) => [agent.id, agent]));
    const ids = new Set([...current.keys(), ...incoming.keys()]);
    const accepted = [...ids].filter((id) => canApplyAgentObservation(id, request)
      && agentSnapshotCovers(request, scope, (incoming.get(id) ?? current.get(id))!.archived));
    for (const id of accepted) {
      const row = incoming.get(id);
      const previous = current.get(id);
      if (row) current.set(id, previous && !row.archived && agentOrderIsProtected(id, request, true)
        ? { ...row, pin_order: previous.pin_order } : row);
      else current.delete(id);
    }
    // A live absence says nothing about the archived partition. Only complete
    // rows or full-range absences protect both partitions of older requests.
    recordAgentSnapshot(request, scope, accepted.filter((id) => incoming.has(id) || scope === "all"));
    const merged = [...current.values()];
    return { agents: merged.filter((agent) => !agent.archived), archivedAgents: merged.filter((agent) => agent.archived),
      ...(scope === "all" && agentReadStatusIsCurrent(request) ? { archivedAgentsRead: { loaded: true, loading: false, invalidated: false, error: null } }
        : scope === "live" && accepted.some((id) => !incoming.has(id)) ? { archivedAgentsRead: { ...s.archivedAgentsRead, invalidated: true } } : {}) };
  }),
  applyAgentOrder: (ids, request) => set((s) => {
    if (!agentRequestIsCurrent(request, s)) return s;
    const current = new Map(s.agents.map((agent) => [agent.id, agent]));
    const accepted = new Set(ids.filter((id) => current.has(id) && canApplyAgent(id, request) && !agentOrderIsProtected(id, request)));
    const ordered = withPinOrder(s.agents, ids);
    recordAgentOrder(accepted, request);
    return { agents: ordered.map((agent) => accepted.has(agent.id) ? agent : current.get(agent.id)!) };
  }),
  confirmAgentOrder: (ids, request) => set((s) => {
    if (!agentRequestIsCurrent(request, s)) return s;
    const accepted = new Set(s.agents.filter((agent) => ids.includes(agent.id) && agent.pinned !== false
      && canApplyAgent(agent.id, request) && !agentOrderIsProtected(agent.id, request)).map((agent) => agent.id));
    recordAgentOrderReceipt(accepted, request);
    const ordered = withPinOrder(s.agents, ids);
    return { agents: ordered.map((agent, index) => accepted.has(agent.id) ? agent : s.agents[index]) };
  }),
  upsertAgent: (agent, request) =>
    set((s) => {
      if ((request && !agentRequestIsCurrent(request, s)) || !canApplyAgent(agent.id, request)) return s;
      recordAgentChanges([agent.id], request);
      const current = new Map([...s.agents, ...s.archivedAgents].map((row) => [row.id, row]));
      const previous = current.get(agent.id);
      current.set(agent.id, request && previous && !agent.archived && agentOrderIsProtected(agent.id, request)
        ? { ...agent, pin_order: previous.pin_order } : agent);
      const rows = [...current.values()];
      return { agents: rows.filter((row) => !row.archived), archivedAgents: rows.filter((row) => row.archived) };
    }),
  removeAgent: (id, request) =>
    set((s) => {
      if ((request && !agentRequestIsCurrent(request, s)) || !canApplyAgent(id, request)) return s;
      recordAgentChanges([id], request);
      return { agents: s.agents.filter((a) => a.id !== id), archivedAgents: s.archivedAgents.filter((a) => a.id !== id) };
    }),
  activeAgentId: null,
  setActiveAgentId: (activeAgentId) => set({ activeAgentId }),

  applications: [],
  archivedApplications: [],
  applicationReads: { live: unreadList(), archived: unreadList() },
  applicationPinOrder: null,
  applicationOrdering: false,

  mainView: "chat",
  activeApplicationId: null,
  editingAgentId: null,
  agentFormGeneration: 0,
  pageTab: "form",
  setPageTab: (pageTab) => set({ pageTab }),
  openApplication: (id) =>
    set((s) => ({
      activeApplicationId: id,
      mainView: "application",
      sidebarOpen: false,
      // Opening it is what "seen" means.
      unseenFailedApplications: s.unseenFailedApplications.filter(
        (a) => a !== id
      ),
    })),
  openApplicationCreate: () =>
    set({
      activeApplicationId: null,
      mainView: "application-create",
      pageTab: "form",
      sidebarOpen: false,
    }),
  openAgentForm: (agentId = null) =>
    set((s) => ({
      mainView: "agent-form",
      editingAgentId: agentId,
      agentFormGeneration: s.agentFormGeneration + 1,
      pageTab: "form",
      activeApplicationId: null,
      sidebarOpen: false,
    })),
  unseenFailedApplications: [],
  noteApplicationFailed: (id) =>
    set((s) =>
      s.unseenFailedApplications.includes(id)
        ? s
        : { unseenFailedApplications: [...s.unseenFailedApplications, id] }
    ),
  openManage: (view) =>
    set({ mainView: view, activeApplicationId: null, sidebarOpen: false }),
  showChat: () =>
    set({ mainView: "chat", activeApplicationId: null, sidebarOpen: false }),

  backendCatalog: { status: "idle" },

  sessions: [],
  sessionRead: unreadList(),
  sessionSuccessors: {},
  setSessions: (sessions) => set({ sessions }),
  archivedSessions: [],
  setArchivedSessions: (archivedSessions) => set({ archivedSessions }),
  updateSessionStatus: (id, status) =>
    set((s) => ({
      sessions: s.sessions.map((sess) =>
        sess.id === id ? { ...sess, status } : sess
      ),
    })),

  activeSessionId: null,
  // Selecting a session is also what leaves an application view — see
  // `mainView` above.
  setActiveSessionId: (id) =>
    set({
      activeSessionId: id,
      mainView: "chat",
      activeApplicationId: null,
      sidebarOpen: false,
    }),
  forgetSession: (id) => set((s) => ({
    activeSessionId: s.activeSessionId === id ? null : s.activeSessionId,
    viewer: s.viewer?.sessionId === id ? null : s.viewer,
    messages: dropKey(s.messages, id), pendingQuestions: dropKey(s.pendingQuestions, id),
    closedQuestionIds: dropKey(s.closedQuestionIds, id), pendingForks: dropKey(s.pendingForks, id),
    questionDrafts: Object.fromEntries(Object.entries(s.questionDrafts).filter(([, draft]) => draft.sessionId !== id)),
    streamingText: dropKey(s.streamingText, id),
    streamingItems: dropKey(s.streamingItems, id),
    subagents: dropKey(s.subagents, id), dispatchStates: dropKey(s.dispatchStates, id),
    contextStates: dropKey(s.contextStates, id),
    dispatchUnavailable: dropKey(s.dispatchUnavailable, id), sessionMembership: dropKey(s.sessionMembership, id),
    oldestSeq: dropKey(s.oldestSeq, id), hasMoreMessages: dropKey(s.hasMoreMessages, id),
    messageWindows: dropKey(s.messageWindows, id), lastAppliedSeq: dropKey(s.lastAppliedSeq, id),
    transcriptReadErrors: dropKey(s.transcriptReadErrors, id),
    bgTasks: dropKey(s.bgTasks, id), bgTaskReadFailures: dropKey(s.bgTaskReadFailures, id),
    research: dropKey(s.research, id), researchReadFailures: dropKey(s.researchReadFailures, id), delegations: dropKey(s.delegations, id),
    wikiJobs: dropKey(s.wikiJobs, id), wikiJobDirty: dropKey(s.wikiJobDirty, id),
    wikiJobReadErrors: dropKey(s.wikiJobReadErrors, id), sessionAuxiliaryErrors: dropKey(s.sessionAuxiliaryErrors, id),
  })),

  lastAppliedSeq: {},

  messages: {},
  addMessage: (sessionId, msg) => {
    let consumers = NO_TEXT_CONSUMERS;
    set((s) => {
      const current = s.messages[sessionId] || [];
      if (typeof msg.seq === "number" && current.some((message) => message.seq === msg.seq)) return s;
      if (msg.role === "assistant" && msg.type === "text" && typeof msg.content === "string" && msg.content.length > 0) {
        consumers = textConsumers(sessionId, s.accountGeneration);
      }
      const messages = [...current];
      insertMessage(messages, normalizeMessage(msg));
      const itemKey = nativeItemKey(msg.runtime_metadata);
      const identity = readAttemptIdentity(msg);
      const root = s.streamingText[sessionId];
      // A completed assistant text block supersedes whatever was streaming:
      // drop the buffer in the same update that appends the real message, so
      // the UI never paints both for a frame.
      const streaming =
        (!itemKey && msg.type === "text" && msg.role === "assistant" && root &&
          (!identity || sameAttempt(identity, root.attempt))) ||
        (msg.type === "result" && sameAttempt(identity, root?.attempt))
          ? dropKey(s.streamingText, sessionId)
          : s.streamingText;
      return {
        ...settledQuestions(s, sessionId, [msg]),
        messages: {
          ...s.messages,
          [sessionId]: messages,
        },
        streamingText: streaming,
        streamingItems: itemKey && msg.type === "text" ? {
          ...s.streamingItems, [sessionId]: dropKey(s.streamingItems[sessionId] || {}, itemKey),
        } : msg.type === "result" && identity ? { ...s.streamingItems, [sessionId]: Object.fromEntries(
          Object.entries(s.streamingItems[sessionId] || {}).filter(([, item]) => !sameAttempt(item.runtime_metadata, identity)))
        } : s.streamingItems,
      };
    });
    // Capture receivers before publication, but deliver after the accepted text
    // is installed. Departure may cancel a receiver during that publication;
    // a newly registered successor must never receive its predecessor's output.
    notifyAcceptedText(consumers);
  },
  setMessages: (sessionId, msgs) =>
    set((s) => {
      const previous = s.messages[sessionId] || [];
      const continuous = continuousTranscript(previous, msgs);
      return {
        ...replaceTranscript(s, sessionId, msgs,
          continuous ? s.messageWindows[sessionId] ?? Symbol() : Symbol(), false),
        // Preserve the rejection watermark while withdrawing installation proof.
        ...(!continuous ? { oldestSeq: dropKey(s.oldestSeq, sessionId),
          hasMoreMessages: { ...s.hasMoreMessages, [sessionId]: false } } : {}),
      };
    }),
  installTranscript: (sessionId, snapshot, streamingBasis) => {
    let applied = false;
    set((s) => {
      if (snapshot.high < (s.lastAppliedSeq[sessionId] ?? -1)) return s;
      const previous = s.messages[sessionId] ?? [];
      const oldest = snapshot.oldestSeq;
      const currentOldest = s.oldestSeq[sessionId];
      // Retain a loaded prefix only when it reaches the snapshot without holes.
      const prefix = previous.filter((message) => typeof message.seq === "number" &&
        oldest !== null && message.seq < oldest).map((message) => message.seq!).sort((a, b) => a - b);
      const preservePrefix = oldest !== null && typeof currentOldest === "number" && currentOldest < oldest &&
        prefix[0] === currentOldest && prefix.length === oldest - currentOldest &&
        prefix.every((seq, index) => seq === currentOldest + index);
      const retained = previous.filter((message) => typeof message.seq === "number" &&
        (message.seq > snapshot.high || preservePrefix && message.seq < oldest!));
      const merged = [...retained, ...snapshot.messages];
      const sequenced = new Map(merged.filter((message) => typeof message.seq === "number").map((message) => [message.seq!, message]));
      const rows = [...sequenced.values()].sort((a, b) => a.seq! - b.seq!);
      rows.push(...snapshot.messages.filter((message) => typeof message.seq !== "number"));
      const nextOldest = preservePrefix ? currentOldest : oldest;
      const hasMore = preservePrefix ? Boolean(s.hasMoreMessages[sessionId]) : snapshot.hasMore;
      const equivalentEmpty = !previous.some((message) => typeof message.seq === "number") &&
        !rows.some((message) => typeof message.seq === "number") && hasTranscriptBaseline(s, sessionId) &&
        s.lastAppliedSeq[sessionId] === snapshot.high && currentOldest === nextOldest && s.hasMoreMessages[sessionId] === hasMore;
      const continuous = equivalentEmpty || hasTranscriptBaseline(s, sessionId) && continuousTranscript(previous, rows);
      const liveHigh = Math.max(-1, ...previous.map((message) => message.seq ?? -1));
      applied = true;
      return {
        ...replaceTranscript(s, sessionId, rows,
          continuous ? s.messageWindows[sessionId] ?? Symbol() : Symbol(), liveHigh >= snapshot.high, streamingBasis),
        oldestSeq: { ...s.oldestSeq, [sessionId]: nextOldest },
        hasMoreMessages: { ...s.hasMoreMessages, [sessionId]: hasMore },
        lastAppliedSeq: { ...s.lastAppliedSeq, [sessionId]: snapshot.high },
        transcriptReadErrors: dropKey(s.transcriptReadErrors, sessionId),
      };
    });
    return applied;
  },

  oldestSeq: {},
  hasMoreMessages: {},
  messageWindows: {},
  transcriptReadErrors: {},
  setTranscriptReadError: (sessionId, error, start) => set((s) => {
    if (start.accountGeneration !== s.accountGeneration || s.transcriptReadErrors[sessionId] === (error ?? undefined)) return s;
    return { transcriptReadErrors: error ? { ...s.transcriptReadErrors, [sessionId]: error } : dropKey(s.transcriptReadErrors, sessionId) };
  }),
  prependMessages: (sessionId, msgs, oldestSeq, hasMore, basis) => {
    let result: MessagePageResult = { state: "obsolete" };
    set((cur) => {
      if (cur.messageWindows[sessionId] !== basis.window || cur.oldestSeq[sessionId] !== basis.beforeSeq) return cur;
      if (msgs.some((message) => !Number.isInteger(message.seq) || message.seq! < 0) ||
          new Set(msgs.map((message) => message.seq)).size !== msgs.length) {
        result = { state: "failed", error: "Invalid history page sequence." }; return cur;
      }
      const page = msgs.filter((message) => message.seq! < basis.beforeSeq).sort((a, b) => a.seq! - b.seq!);
      if (page.length && oldestSeq !== null && oldestSeq !== page[0].seq) {
        result = { state: "failed", error: "History page cursor does not match its rows." }; return cur;
      }
      if (page.length === 0 && hasMore) {
        result = { state: "failed", error: "History page did not extend the window." }; return cur;
      }
      const previous = cur.messages[sessionId] || [];
      const known = new Set(previous.map((message) => message.seq));
      const fresh = page.filter((message) => !known.has(message.seq));
      const messages = fresh.length ? [...previous] : previous;
      fresh.forEach((message) => insertMessage(messages, normalizeMessage(message)));
      result = { state: "merged", added: fresh.length };
      return {
      ...settledQuestions(cur, sessionId, fresh),
      messages: {
        ...cur.messages,
        [sessionId]: messages,
      },
      oldestSeq: {
        ...cur.oldestSeq,
        [sessionId]: page[0]?.seq ?? cur.oldestSeq[sessionId],
      },
      streamingText: hasTerminalFor(page, cur.streamingText[sessionId]?.attempt) ? dropKey(cur.streamingText, sessionId) : cur.streamingText,
      streamingItems: { ...cur.streamingItems, [sessionId]: Object.fromEntries(
        Object.entries(cur.streamingItems[sessionId] || {}).filter(([key, item]) =>
          !hasTerminalFor(page, item.runtime_metadata) && !page.some((message) =>
            message.role === "assistant" && message.type === "text" && nativeItemKey(message.runtime_metadata) === key))) },
      hasMoreMessages: { ...cur.hasMoreMessages, [sessionId]: hasMore },
    }; });
    return result;
  },

  // Text the model is producing right now, accumulated from `assistant_delta`
  // frames. Deliberately never persisted and never part of `messages`: the
  // completed block that follows is the real message, and this is only what
  // lets the UI paint before it arrives.
  streamingText: {},
  streamingItems: {},
  appendStreamingText: (sessionId, chunk, metadata) => {
    let consumers = NO_TEXT_CONSUMERS;
    set((s) => {
      const key = nativeItemKey(metadata);
      const identity = readAttemptIdentity(metadata);
      if (identity && !sameAttempt(identity, s.activeAttempts[sessionId])) return s;
      const messages = s.messages[sessionId] ?? [];
      if (messages.length && identity) {
        const admission = streamingAdmission(messages);
        if (admission.terminals.has(attemptKey(identity)) || key && admission.collectedItems.has(key)) return s;
      }
      if (chunk.length > 0) consumers = textConsumers(sessionId, s.accountGeneration);
      if (key && metadata) {
        const previous = s.streamingItems[sessionId]?.[key];
        const { content, truncated } = streamingPreview(previous, chunk, 16 * 1024);
        const item = previous && content === previous.content && sameJsonValue(previous.runtime_metadata, metadata)
          ? previous : { content, runtime_metadata: metadata };
        if (!chunk && item === previous) return s;
        streamingFacts.set(item, { truncated, arrival: Symbol() });
        if (item === previous) return s;
        const items = { ...s.streamingItems[sessionId], [key]: item };
        while (Object.keys(items).length > 32 || streamingBytes(items) > 256 * 1024) delete items[Object.keys(items)[0]];
        return { streamingItems: { ...s.streamingItems, [sessionId]: items } };
      }
      const root = s.streamingText[sessionId];
      const attempt = identity ?? s.activeAttempts[sessionId] ?? root?.attempt;
      const previous = !attempt || sameAttempt(attempt, root?.attempt) ? root : undefined;
      if (!chunk && previous === root) return s;
      const { content, truncated } = streamingPreview(previous, chunk, 64 * 1024);
      const preview = previous && content === previous.content ? previous : { content, attempt };
      // The ordered live channel sends the delta before its durable block.
      // Preserve the known history frontier as of the first observed chunk.
      const afterSeq = previous ? streamingFacts.get(previous)?.afterSeq
        : messages.reduce((high, message) => Math.max(high, message.seq ?? -1), s.lastAppliedSeq[sessionId] ?? -1);
      streamingFacts.set(preview, { truncated, arrival: Symbol(), afterSeq });
      if (preview === previous) return s;
      return {
        streamingText: { ...s.streamingText, [sessionId]: preview },
      };
    });
    notifyAcceptedText(consumers);
  },
  clearStreamingText: (sessionId, attempt) => set((s) => {
    const previews = attempt ? retainStreamingPreviews(s, sessionId, (owner) => !sameAttempt(attempt, owner)) : {
      streamingText: dropKey(s.streamingText, sessionId),
      streamingItems: dropKey(s.streamingItems, sessionId),
    };
    return previews.streamingText === s.streamingText && previews.streamingItems === s.streamingItems ? s : previews;
  }),

  subagents: {},
  upsertSubagent: (sessionId, run) =>
    set((s) => {
      const key = run.card_id || run.tool_call_id;
      if (!key) return s;
      const forSession = s.subagents[sessionId] || {};
      const prev = forSession[key];
      // Each update is partial by design — a status patch carries no name, a
      // summary carries no counters — so the merge keeps whatever this one
      // doesn't restate. The step trail is built here rather than sent: the
      // wire carries the step a run is *on*, and three busy sub-agents would
      // otherwise repeat their whole history every second.
      const steps = run.steps?.length
        ? run.steps
        : appendStep(prev?.steps, run.description);
      const updated = { ...prev, ...prune(run), steps };
      if (run.card_id && prev && ["completed", "failed", "cancelled", "interrupted"].includes(prev.status)) updated.status = prev.status;
      subagentUpdates.set(updated, Symbol());
      return {
        subagents: {
          ...s.subagents,
          [sessionId]: Object.fromEntries(Object.entries({ ...forSession, [key]: updated }).slice(-64)),
        },
      };
    }),
  setSubagents: (sessionId, runs, baseline, advanceOnly) =>
    set((s) => {
      const current = s.subagents[sessionId];
      const merged = mergeCardSnapshot(runs.filter((r) => r.card_id || r.tool_call_id), Object.values(current ?? {}),
        baseline === undefined ? undefined : Object.values(baseline), (r) => (r.card_id || r.tool_call_id)!,
        subagentUpdates, (r) => r.status === "running", advanceOnly);
      const next = Object.fromEntries(merged.slice(-64).map((r) => [(r.card_id || r.tool_call_id)!, r]));
      // Compare after merging event provenance. Keep the observed rows when
      // an equivalent snapshot only inherits their existing markers.
      if (current && sameJsonValue(Object.entries(current), Object.entries(next))) return s;
      return { subagents: { ...s.subagents, [sessionId]: next } };
    }),

  schedules: [],
  scheduleRead: unreadList(),

  credentials: [],
  credentialRead: unreadList(),

  connectorCatalog: [],
  connectorCatalogRead: unreadList(),
  connectorInstallations: [],
  connectorRead: unreadList(),

  wikiJobs: {},
  wikiJobReadErrors: {},
  wikiJobDirty: {},
  invalidateWikiJob: (sessionId, target) => set((state) => {
    const key = `${target.vault_id}/${target.job_id}`;
    const dirty = state.wikiJobDirty[sessionId] || {};
    if (dirty[key] && dirty[key].state_version >= target.state_version) return state;
    return { wikiJobDirty: { ...state.wikiJobDirty, [sessionId]: { ...dirty, [key]: target } } };
  }),
  mergeWikiJobs: (sessionId, cards) => set((state) => {
    const current = state.wikiJobs[sessionId] || [];
    const values = new Map(current.map((card) => [`${card.vault_id}/${card.job_id}`, card]));
    let changed = false;
    const dirty = { ...state.wikiJobDirty[sessionId] };
    let refreshed = false;
    for (const card of cards) {
      const key = `${card.vault_id}/${card.job_id}`;
      const previous = values.get(key);
      if (!previous || card.state_version > previous.state_version) {
        values.set(key, card);
        changed = true;
      } else if (card.current_state !== undefined && card.current_state !== previous.current_state
          && (card.current_state === "unavailable" || card.state_version === previous.state_version)) {
        // Permissions/registration can change without changing the job. A
        // retained summary may be older than facts this browser already read.
        values.set(key, { ...previous, current_state: card.current_state });
        changed = true;
      }
      if (dirty[key] && card.state_version >= dirty[key].state_version) {
        delete dirty[key];
        refreshed = true;
      }
    }
    if (!changed && !refreshed) return state;
    return { wikiJobs: { ...state.wikiJobs, [sessionId]: [...values.values()]
      .sort((a, b) => b.received_at.localeCompare(a.received_at)) },
      wikiJobDirty: { ...state.wikiJobDirty, [sessionId]: dirty } };
  }),
  dispatchStates: {},
  dispatchUnavailable: {},
  setDispatchUnavailable: (sessionId, unavailable, start) => set((s) => {
    if (start && (start.accountGeneration !== s.accountGeneration ||
        start.dispatchGeneration !== s.dispatchGeneration ||
        start.dispatchStates[sessionId] !== s.dispatchStates[sessionId] ||
        start.executionObservations[sessionId] !== s.executionObservations[sessionId])) return s;
    const errors = { ...s.dispatchUnavailable };
    if (unavailable) errors[sessionId] = true;
    else delete errors[sessionId];
    return { dispatchUnavailable: errors };
  }),
  dispatchGeneration: null,
  retiredDispatchGenerations: [],
  sessionMembership: {},
  applyDispatchState: (sessionId, value, start) => {
    const snapshot = readDispatchState(value);
    if (!snapshot) {
      const generation = value && typeof value === "object" && "generation" in value ? value.generation : null;
      if (typeof generation === "string" && get().retiredDispatchGenerations.includes(generation)) return false;
      get().setDispatchUnavailable(sessionId, true, start);
      return false;
    }
    let applied = false;
    set((s) => {
      const patch = dispatchUpdate(s, sessionId, snapshot, start);
      if (!patch) return s;
      applied = true;
      const observations = patch.executionObservations ?? s.executionObservations;
      return { ...patch, executionObservations: { ...observations, [sessionId]: (observations[sessionId] ?? 0) + 1 } };
    });
    return applied;
  },

  pendingForks: {},
  setPendingFork: (sessionId, label) =>
    set((s) => ({
      pendingForks: { ...s.pendingForks, [sessionId]: { label } },
    })),
  clearPendingFork: (sessionId) =>
    set((s) => {
      if (!s.pendingForks[sessionId]) return s;
      const next = { ...s.pendingForks };
      delete next[sessionId];
      return { pendingForks: next };
    }),

  pendingQuestions: {},
  questionDrafts: {},
  closedQuestionIds: {},
  setPendingQuestions: (sessionId, qs) =>
    set((s) => {
      const pending = qs.filter((q) => !s.closedQuestionIds[sessionId]?.[q.question_id]);
      const next = { ...s.pendingQuestions };
      if (pending.length === 0) delete next[sessionId];
      else next[sessionId] = pending;
      return { pendingQuestions: next };
    }),
  addPendingQuestion: (sessionId, q) =>
    set((s) => {
      if (s.closedQuestionIds[sessionId]?.[q.question_id]) return s;
      return {
        pendingQuestions: {
          ...s.pendingQuestions,
          [sessionId]: [...(s.pendingQuestions[sessionId] || []).filter((current) =>
            current.question_id !== q.question_id
          ), q],
        },
      };
    }),
  removePendingQuestion: (sessionId, questionId) =>
    set((s) => {
      const cur = s.pendingQuestions[sessionId] || [];
      const filtered = cur.filter((q) => q.question_id !== questionId);
      const next = { ...s.pendingQuestions };
      if (filtered.length === 0) delete next[sessionId];
      else next[sessionId] = filtered;
      return {
        pendingQuestions: next,
        closedQuestionIds: {
          ...s.closedQuestionIds,
          [sessionId]: { ...s.closedQuestionIds[sessionId], [questionId]: true },
        },
      };
    }),

  connected: false,
  setConnected: (c) => set({ connected: c }),

  viewer: null,
  openViewer: (sessionId, path, options) => set({
    viewer: { id: ++nextFileViewerId, sessionId, path, ...options },
  }),
  closeViewer: () => set({ viewer: null }),

  bgTasks: {},
  bgTaskReadFailures: {},
  researchReadFailures: {},
  setAuxiliaryTaskFailure: (kind, sessionId, taskId, failed) => set((s) => {
    const key = kind === "background" ? "bgTaskReadFailures" : "researchReadFailures";
    const failures = s[key];
    const ids = failures[sessionId] ?? [];
    if (ids.includes(taskId) === failed) return s;
    const remaining = failed ? [...ids, taskId] : ids.filter(id => id !== taskId);
    return { [key]: remaining.length ? { ...failures, [sessionId]: remaining } : dropKey(failures, sessionId) };
  }),
  clearAuxiliaryTaskFailures: (sessionId) => set((s) => s.bgTaskReadFailures[sessionId] || s.researchReadFailures[sessionId]
    ? { bgTaskReadFailures: dropKey(s.bgTaskReadFailures, sessionId), researchReadFailures: dropKey(s.researchReadFailures, sessionId) } : s),
  sessionAuxiliaryErrors: {},
  setSessionAuxiliaryError: (sessionId, kind, error) => set((s) => {
    const current = s.sessionAuxiliaryErrors[sessionId];
    if ((current?.[kind] ?? null) === error) return s;
    const next = { ...current };
    if (error) next[kind] = error;
    else delete next[kind];
    const errors = { ...s.sessionAuxiliaryErrors };
    if (Object.keys(next).length) errors[sessionId] = next;
    else delete errors[sessionId];
    return { sessionAuxiliaryErrors: errors };
  }),
  upsertBgTask: (sessionId, task, baseline, advanceOnly = false) =>
    set((s) => {
      const current = s.bgTasks[sessionId] || [];
      const idx = current.findIndex((t) => t.id === task.id);
      const updated = idx >= 0 ? mergeBgTaskSnapshot(current[idx], task, baseline, advanceOnly) : task;
      const next = idx >= 0
        ? [...current.slice(0, idx), updated, ...current.slice(idx + 1)]
        : [...current, updated];
      return { bgTasks: { ...s.bgTasks, [sessionId]: next } };
    }),
  applyBgTaskEvent: (event) =>
    set((s) => {
      const sessionId = event.session_id;
      const current = s.bgTasks[sessionId] || [];
      const idx = current.findIndex((task) => task.id === event.task_id);
      const previous = current[idx];
      let updated: BgTask;

      if (event.type === "bg_started") {
        if (previous && previous.status !== "pending") return s;
        const started = {
          status: "running" as const,
          command: bgTaskPreviewText(event.command, 120),
          started_at: event.started_at,
          ...(event.description !== undefined && { description: event.description == null ? null : bgTaskPreviewText(event.description, 120) }),
          ...(event.idle_timeout_seconds !== undefined && { idle_timeout_seconds: event.idle_timeout_seconds }),
          ...(event.timeout_seconds !== undefined && { timeout_seconds: event.timeout_seconds }),
        };
        updated = previous ? mergeBgTask(previous, started, { event: event.type }) : {
          id: event.task_id, session_id: sessionId, description: null, completed_at: null,
          ...started,
        };
      } else {
        if (!previous) return s; // The observation layer reads missing tasks by id.
        if (event.type === "bg_completed") {
          updated = mergeBgTask(previous, {
            status: event.status,
            completed_at: event.completed_at,
            ...(event.exit_code !== undefined && { exit_code: event.exit_code }),
            ...(event.truncated !== undefined && { truncated: event.truncated }),
            ...(event.stop_reason !== undefined && { stop_reason: event.stop_reason == null ? null : bgTaskPreviewText(event.stop_reason, 120) }),
            ...(event.idle_timeout_seconds !== undefined && { idle_timeout_seconds: event.idle_timeout_seconds }),
            ...(event.timeout_seconds !== undefined && { timeout_seconds: event.timeout_seconds }),
          }, { event: event.type });
        } else {
          updated = mergeBgTask(previous, {
            ...(event.delivery_disposition !== undefined && { delivery_disposition: event.delivery_disposition }),
            ...(event.delivery_error !== undefined && { delivery_error: event.delivery_error == null ? null : bgTaskPreviewText(event.delivery_error, 160) }),
          }, { event: event.type });
        }
      }
      if (updated === previous) return s;
      const rows = idx < 0 ? [...current, updated] : current.map((task, i) => i === idx ? updated : task);
      return { bgTasks: { ...s.bgTasks, [sessionId]: rows } };
    }),
  setBgTasks: (sessionId, tasks, baseline, claim) =>
    set((s) => {
      const current = s.bgTasks[sessionId] || [];
      const previous = new Map(current.map((task) => [task.id, task]));
      const start = baseline && new Map(baseline.map((task) => [task.id, task]));
      const rows = tasks.map((task) => {
        const existing = previous.get(task.id);
        const advanceOnly = claim ? !claim(task.id) : false;
        return existing ? mergeBgTaskSnapshot(existing, task, start?.get(task.id), advanceOnly) : task;
      });
      const incoming = new Set(tasks.map((task) => task.id));
      // The server only returns its most recent window. Omission does not
      // retire a task that a card may already be observing.
      for (const task of current) if (!incoming.has(task.id)) rows.push(task);
      return { bgTasks: { ...s.bgTasks, [sessionId]: rows } };
    }),

  research: {},
  initializeResearch: (sessionId, job) => set((s) => updateResearch(s, sessionId, job.id, (previous) =>
    previous ? mergeResearchJob(previous, job, { kind: "creation" }) : initializeResearchJob(job, true))),
  confirmResearchSeen: (sessionId, jobId, receipt) => set((s) => updateResearch(s, sessionId, jobId, (job) =>
    job && confirmResearchJobSeen(job, receipt))),
  applyResearchEvent: (event) => set((s) => updateResearch(s, event.session_id, event.research_id, (previous) => {
    if (event.type === "research_started") {
      return previous ?? (event.question ? initializeResearchJob({ id: event.research_id, session_id: event.session_id,
        question: event.question, status: "running", phase: "scope" }, true) : undefined);
    }
    return previous && mergeResearchJob(previous, researchEventPatch(event), { kind: "event" });
  })),
  applyResearchControl: (sessionId, job, baseline) => set((s) => updateResearch(s, sessionId, job.id, (previous) =>
    previous?.status === "running" ? mergeResearchJob(previous, job, { kind: "control", baseline }) : previous)),
  applyResearchDetail: (sessionId, job, baseline, advanceOnly) => set((s) => updateResearch(s, sessionId, job.id, (previous) =>
    confirmResearchDetail(previous ? mergeResearchJob(previous, job, { kind: "snapshot", baseline, advanceOnly }) : initializeResearchJob(job)))),
  setResearch: (sessionId, jobs, baseline, canReplace) => set((s) => {
    const current = s.research[sessionId] ?? [];
    const known = new Map(current.map((job) => [job.id, job]));
    const start = new Map((baseline ?? current).map((job) => [job.id, job]));
    const rows = new Map<string, ResearchJob>();
    for (const job of jobs) {
      const previous = known.get(job.id);
      const advanceOnly = canReplace ? !canReplace(job.id) : false;
      rows.set(job.id, previous ? mergeResearchJob(previous, job, { kind: "snapshot", baseline: start.get(job.id), advanceOnly })
        : initializeResearchJob(job));
    }
    for (const job of current) {
      const before = start.get(job.id);
      if (!rows.has(job.id) && (researchConfirmedByDetail(job) || !before || researchChangedSince(job, before) ||
        before.status === "running" && job.status !== "running")) rows.set(job.id, job);
    }
    return { research: { ...s.research, [sessionId]: [...rows.values()] } };
  }),

  delegations: {},
  setDelegations: (parentSessionId, ds, baseline, advanceOnly) =>
    set((s) => ({
      delegations: { ...s.delegations, [parentSessionId]: mergeCardSnapshot(
        ds, s.delegations[parentSessionId] || [], baseline, (round) => `${round.delegation_id}:${round.work_id}`,
        delegationUpdates, (round) => round.state === "running", advanceOnly,
      ) },
    })),
  upsertDelegation: (parentSessionId, round) => set((s) => {
    const updated = { ...round };
    delegationUpdates.set(updated, Symbol());
    const current = s.delegations[parentSessionId] || [];
    const found = current.some((item) => item.delegation_id === round.delegation_id && item.work_id === round.work_id);
    return { delegations: { ...s.delegations, [parentSessionId]: found ? current.map((item) =>
      item.delegation_id === round.delegation_id && item.work_id === round.work_id ? updated : item) : [updated, ...current] } };
  }),

  showDelegations: localStorage.getItem("octopus_show_delegations") === "true",
  setShowDelegations: (v) => {
    if (v) localStorage.setItem("octopus_show_delegations", "true");
    else localStorage.removeItem("octopus_show_delegations");
    set({ showDelegations: v });
  },

  sidebarCollapsed: localStorage.getItem("octopus_sidebar_collapsed") === "true",
  setSidebarCollapsed: (v) => {
    if (v) localStorage.setItem("octopus_sidebar_collapsed", "true");
    else localStorage.removeItem("octopus_sidebar_collapsed");
    set({ sidebarCollapsed: v });
  },

  // Never persisted: a drawer that reopened itself on load would be a
  // wall between you and the app every time you launch it.
  sidebarOpen: false,
  setSidebarOpen: (v) => set({ sidebarOpen: v }),
}));
