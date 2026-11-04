import { create } from "zustand";
import { mergeAuxiliarySnapshot, mergeCardSnapshot } from "../lib/cardSnapshots";
import { agentRequestIsCurrent, beginAgentRequest, canApplyAgent, endAgentRequest, recordAgentChanges, recordAgentSnapshot, type AgentRequest } from "../lib/agentUpdates";
import { withPinOrder } from "../lib/sidebarPins";
import { unreadList, type ListReadState } from "../lib/listState";
import type { ControlCommandStatus } from "../lib/submission";
import type { QuestionDraft } from "../lib/questionDrafts";
import { normalizeMessage, type MessageInput } from "../lib/turnOutcome";
import { readActiveAttempt, readDispatchState, type ActiveAttempt, type DispatchState } from "../lib/sessionDispatch";
import { nativeItemKey } from "../lib/nativeOutput";
import type {
  AgentRead as ApiAgentRead,
  ApplicationRead as ApiApplicationRead,
  AttachmentMetadata as ApiAttachmentMetadata,
  BackendKind as ApiBackendKind,
  BgTaskResponse as ApiBgTaskResponse,
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
  ToolPolicySupport,
  WikiJobCard,
} from "../api";

// Provenance for immutable per-job updates, including WS and API receipts.
// Snapshot rows stay unmarked. Weak references retain no second task state.
const researchUpdates = new WeakMap<ResearchJob, symbol>();
const bgUpdates = new WeakMap<BgTask, symbol>();
const subagentUpdates = new WeakMap<SubagentRun, symbol>();
const delegationUpdates = new WeakMap<Delegation, symbol>();
// Only delivery events advance this observation. Entity/list replacement does
// not prove a new delivery fact; immutable copies retain their event marker.
const bgDeliveryUpdates = new WeakMap<BgTask, symbol>();

// Re-export contract types under the names the rest of the frontend
// already uses. Source of truth is `web/src/api/contracts.ts`, regenerated
// from FastAPI's openapi.json via `bun run generate:contracts`.
export type SessionStatus = ApiSessionStatus;
export type SessionInfo = ApiSessionInfo;
export type SubagentRun = ApiSubagentRun;
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
  setAgents: (a: Agent[]) => void;
  beginAgentRequest: (kind?: AgentRequest["kind"]) => AgentRequest;
  endAgentRequest: (request: AgentRequest) => void;
  applyAgentSnapshot: (agents: Agent[], request: AgentRequest) => void;
  applyAgentOrder: (ids: string[], request: AgentRequest) => void;
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

  // Which AI backends this host can run (GET /api/backends). 'claude-code'
  // is always present; 'codex' only when the binary resolves. Drives the
  // backend selector in the session-create form (codex-backend.md §6).
  availableBackends: string[];
  defaultBackend: BackendKind;
  setDefaultBackend: (backend: BackendKind) => void;
  setAvailableBackends: (b: string[]) => void;
  // Model shortlist per backend, from the same `GET /api/backends`. Feeds
  // `/model`'s picker; empty for a backend whose accepted names this build
  // does not claim to know (see RuntimeProfile.models).
  backendModels: Record<string, string[]>;
  setBackendModels: (m: Record<string, string[]>) => void;
  backendToolPolicies: Record<string, ToolPolicySupport>;
  setBackendToolPolicies: (policies: Record<string, ToolPolicySupport>) => void;

  sessions: SessionInfo[];
  sessionRead: ListReadState;
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
  setMessages: (sessionId: string, msgs: MessageInput[], preserveStreaming?: boolean, nativeBaseline?: Record<string, StreamingItem>) => void;
  // The transcript arrives windowed to the most recent messages
  // (polish-2026-09.md §4 B2): `oldestSeq` is the lowest seq held for a
  // session and the cursor the next page asks from, `hasMoreMessages` whether
  // there is anything before it. Both come from the snapshot, so a session
  // whose whole transcript fits has `hasMoreMessages: false` and never asks.
  oldestSeq: Record<string, number | null>;
  hasMoreMessages: Record<string, boolean>;
  messageWindows: Record<string, symbol | undefined>;
  setMessageWindow: (
    sessionId: string,
    oldestSeq: number | null,
    hasMore: boolean
  ) => void;
  // Accept a page only into the window it extends, in one store update.
  prependMessages: (
    sessionId: string,
    msgs: MessageInput[],
    oldestSeq: number | null,
    hasMore: boolean,
    basis: MessagePageBasis,
  ) => MessagePageResult;

  // Per-session WS-event dedup baseline. Set when a snapshot is loaded
  // (`/api/sessions/{id}` returns `next_message_seq`); the WS handler
  // drops any event whose `seq <= lastAppliedSeq[sessionId]` so a
  // reconnect-refetch can't stomp an event that arrived between the
  // snapshot's SQL and `setMessages` (the original race).
  lastAppliedSeq: Record<string, number>;
  setLastAppliedSeq: (sessionId: string, seq: number) => void;

  schedules: Schedule[];
  scheduleRead: ListReadState;

  credentials: CredentialInfo[];
  setCredentials: (c: CredentialInfo[]) => void;

  // Connector catalog and installations (connectors.md). AgentFormPage owns
  // its enablement draft, backed by the agent connector routes.
  connectorCatalog: ConnectorCatalogEntry[];
  setConnectorCatalog: (c: ConnectorCatalogEntry[]) => void;
  connectorInstallations: ConnectorInstallationInfo[];
  setConnectorInstallations: (c: ConnectorInstallationInfo[]) => void;
  upsertConnectorInstallation: (c: ConnectorInstallationInfo) => void;
  removeConnectorInstallation: (id: string) => void;

  // Text the model is streaming right now, per session, accumulated from
  // `assistant_delta` frames and dropped the moment the completed block
  // arrives. Never persisted, never part of `messages`.
  streamingText: Record<string, string>;
  streamingItems: Record<string, Record<string, StreamingItem>>;
  appendStreamingText: (sessionId: string, chunk: string, metadata?: RuntimeMetadata) => void;
  clearStreamingText: (sessionId: string) => void;

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

  // FileViewerDialog is mounted at the App level and reads this slot.
  // null = closed; non-null = open and fetching the named file. Set
  // by ChatView when the `/showme` resolver returns a concrete path.
  viewer: { sessionId: string; path: string } | null;
  openViewer: (sessionId: string, path: string) => void;
  closeViewer: () => void;

  // Cross-turn background tasks. Keyed by sessionId → list of tasks
  // (most-recent last as they arrive over WS / from snapshot fetch).
  // The BgTaskChip in chat looks each task up by id; it lives next
  // to the `mcp__bg__run` tool_use block that started it.
  bgTasks: Record<string, BgTask[]>;
  upsertBgTask: (sessionId: string, task: BgTask, baseline?: BgTask | null, advanceOnly?: boolean) => void;
  updateBgTaskDelivery: (sessionId: string, taskId: string, delivery: Pick<BgTask, "delivery_disposition" | "delivery_error">) => void;
  setBgTasks: (sessionId: string, tasks: BgTask[], baseline?: BgTask[], advanceOnly?: boolean, deliveryCurrent?: (id: string) => boolean) => void;
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
  upsertResearch: (sessionId: string, job: Partial<ResearchJob> & { id: string }) => void;
  setResearch: (sessionId: string, jobs: ResearchJob[], baseline?: ResearchJob[], advanceOnly?: boolean) => void;
}

export interface ResearchJob {
  id: string;
  session_id: string;
  question: string;
  status: "running" | "completed" | "failed" | "cancelled" | "interrupted";
  phase: string | null;        // scope | search | verify | synthesize | done
  detail?: string;
  created_at?: string;
  report_path?: string | null;
  counts?: Record<string, number>;
  sources?: string[];
  verified?: number;
  unverified?: number;
  delivery_receipt?: {
    request_id: string;
    version: number;
    stage: "queued" | "processing" | "processed" | "failed" | "recording_unconfirmed" | "unknown";
    error?: string | null;
    browser_confirmed_version?: number;
    browser_confirmed_at?: string;
  } | null;
  error?: string | null;
  injection_status?: "pending" | "delivered" | "failed" | "unknown";
}

// HTTP responses are complete; live events may omit limits and delivery facts.
export type BgTask = Omit<ApiBgTaskResponse,
  "work_id" | "idle_timeout_seconds" | "timeout_seconds" | "stop_reason" | "delivery_disposition" | "delivery_error"
> & Partial<Pick<ApiBgTaskResponse,
  "idle_timeout_seconds" | "timeout_seconds" | "stop_reason" | "delivery_disposition" | "delivery_error"
>>;

/** Execution, complete output and delivery have independent evidence. Both
 * list and detail responses pass through this same per-task boundary. */
function mergeBgTask(task: BgTask, previous: BgTask | undefined, baseline: BgTask | null | undefined,
  advanceOnly: boolean, event = false): BgTask {
  const updated = { ...previous, ...task };
  const deliveryUpdate = previous && bgDeliveryUpdates.get(previous);
  if (previous && baseline !== undefined) {
    if (advanceOnly || deliveryUpdate !== undefined && deliveryUpdate !== (baseline ? bgDeliveryUpdates.get(baseline) : undefined)) {
      updated.delivery_disposition = previous.delivery_disposition;
      updated.delivery_error = previous.delivery_error;
    }
    const terminal = previous.status !== "running" && previous.status !== "pending";
    const changed = bgUpdates.get(previous) !== (baseline ? bgUpdates.get(baseline) : undefined);
    if (terminal && (advanceOnly || changed || previous.status !== task.status) || previous.status === "running" && task.status === "pending") {
      for (const field of ["status", "exit_code", "completed_at", "stop_reason", "truncated"] as const) {
        if (previous[field] !== undefined) Object.assign(updated, { [field]: previous[field] });
      }
    }
    for (const field of ["stdout", "stderr"] as const) {
      if ((previous[field]?.length ?? 0) > (updated[field]?.length ?? 0)) updated[field] = previous[field];
    }
  }
  if (deliveryUpdate) bgDeliveryUpdates.set(updated, deliveryUpdate);
  const marker = event ? Symbol() : previous && bgUpdates.get(previous);
  if (marker) bgUpdates.set(updated, marker);
  return updated;
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
    ...(changed ? { activeAttempts: {}, executionObservations: {}, closedQuestionIds: {}, pendingQuestions: {},
      sessionMembership: {}, sessionAuxiliaryErrors: {}, streamingItems: {} } : {}),
    retiredDispatchGenerations: changed ? [...s.retiredDispatchGenerations, s.dispatchGeneration!] : s.retiredDispatchGenerations,
    // Equal revision confirms content and invalidates earlier read failures.
    dispatchStates: { ...(changed ? {} : s.dispatchStates),
      [sessionId]: previous?.revision === snapshot.revision ? { ...previous } : snapshot },
    dispatchUnavailable: errors,
  };
}

export const useSessionStore = create<SessionStore>((set, get) => ({
  activeAttempts: {},
  executionObservations: {},
  setActiveAttempt: (sessionId, attempt) => set((s) => ({
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
      applied = true;
      return { ...patch, activeAttempts: active ? { ...attempts, [sessionId]: active } : dropKey(attempts, sessionId) };
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
  setAgents: (agents) => {
    recordAgentChanges(new Set([...get().agents, ...agents].map((agent) => agent.id)));
    set({ agents });
  },
  beginAgentRequest: (kind) => beginAgentRequest(get(), kind),
  endAgentRequest,
  applyAgentSnapshot: (rows, request) => set((s) => {
    if (!agentRequestIsCurrent(request, s)) return s;
    const incoming = new Map(rows.filter((agent) => !agent.archived).map((agent) => [agent.id, agent]));
    const ids = new Set([...s.agents, ...rows].map((agent) => agent.id));
    const current = new Map(s.agents.map((agent) => [agent.id, agent]));
    const accepted = [...ids].filter((id) => canApplyAgent(id, request));
    for (const id of accepted) {
      const row = incoming.get(id);
      if (row) current.set(id, row);
      else current.delete(id);
    }
    recordAgentChanges(accepted, request);
    recordAgentSnapshot(request);
    return { agents: [...current.values()] };
  }),
  applyAgentOrder: (ids, request) => set((s) => {
    if (!agentRequestIsCurrent(request, s)) return s;
    const current = new Map(s.agents.map((agent) => [agent.id, agent]));
    const accepted = new Set(ids.filter((id) => current.has(id) && canApplyAgent(id, request)));
    const ordered = withPinOrder(s.agents, ids);
    recordAgentChanges(accepted, request, true);
    return { agents: ordered.map((agent) => accepted.has(agent.id) ? agent : current.get(agent.id)!) };
  }),
  upsertAgent: (agent, request) =>
    set((s) => {
      if ((request && !agentRequestIsCurrent(request, s)) || !canApplyAgent(agent.id, request)) return s;
      recordAgentChanges([agent.id], request);
      if (agent.archived) return { agents: s.agents.filter((row) => row.id !== agent.id) };
      const idx = s.agents.findIndex((a) => a.id === agent.id);
      const agents =
        idx >= 0
          ? [...s.agents.slice(0, idx), agent, ...s.agents.slice(idx + 1)]
          : [...s.agents, agent];
      return { agents };
    }),
  removeAgent: (id, request) =>
    set((s) => {
      if ((request && !agentRequestIsCurrent(request, s)) || !canApplyAgent(id, request)) return s;
      recordAgentChanges([id], request);
      return { agents: s.agents.filter((a) => a.id !== id) };
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
    set({
      mainView: "agent-form",
      editingAgentId: agentId,
      pageTab: "form",
      activeApplicationId: null,
      sidebarOpen: false,
    }),
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

  availableBackends: ["claude-code"],
  defaultBackend: "claude-code",
  setDefaultBackend: (defaultBackend) => set({ defaultBackend }),
  setAvailableBackends: (availableBackends) => set({ availableBackends }),
  backendModels: {},
  setBackendModels: (backendModels) => set({ backendModels }),
  backendToolPolicies: {},
  setBackendToolPolicies: (backendToolPolicies) => set({ backendToolPolicies }),

  sessions: [],
  sessionRead: unreadList(),
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
    streamingText: dropKey(s.streamingText, id), streamingItems: dropKey(s.streamingItems, id),
    subagents: dropKey(s.subagents, id), dispatchStates: dropKey(s.dispatchStates, id),
    dispatchUnavailable: dropKey(s.dispatchUnavailable, id), sessionMembership: dropKey(s.sessionMembership, id),
    oldestSeq: dropKey(s.oldestSeq, id), hasMoreMessages: dropKey(s.hasMoreMessages, id),
    messageWindows: dropKey(s.messageWindows, id), lastAppliedSeq: dropKey(s.lastAppliedSeq, id),
    bgTasks: dropKey(s.bgTasks, id), research: dropKey(s.research, id), delegations: dropKey(s.delegations, id),
    wikiJobs: dropKey(s.wikiJobs, id), wikiJobDirty: dropKey(s.wikiJobDirty, id),
    wikiJobReadErrors: dropKey(s.wikiJobReadErrors, id), sessionAuxiliaryErrors: dropKey(s.sessionAuxiliaryErrors, id),
  })),

  lastAppliedSeq: {},
  setLastAppliedSeq: (sessionId, seq) =>
    set((s) => {
      const current = s.lastAppliedSeq[sessionId] ?? -1;
      if (seq <= current) return s;
      return { lastAppliedSeq: { ...s.lastAppliedSeq, [sessionId]: seq } };
    }),

  messages: {},
  addMessage: (sessionId, msg) =>
    set((s) => {
      const itemKey = nativeItemKey(msg.runtime_metadata);
      // A completed assistant text block supersedes whatever was streaming:
      // drop the buffer in the same update that appends the real message, so
      // the UI never paints both for a frame.
      const streaming =
        !itemKey && msg.type === "text" && msg.role === "assistant" && s.streamingText[sessionId]
          ? dropKey(s.streamingText, sessionId)
          : s.streamingText;
      return {
        messages: {
          ...s.messages,
          [sessionId]: [...(s.messages[sessionId] || []), normalizeMessage(msg)],
        },
        streamingText: streaming,
        streamingItems: itemKey && msg.type === "text" ? {
          ...s.streamingItems, [sessionId]: dropKey(s.streamingItems[sessionId] || {}, itemKey),
        } : s.streamingItems,
      };
    }),
  setMessages: (sessionId, msgs, preserveStreaming = false, nativeBaseline) =>
    set((s) => {
      const previous = s.messages[sessionId] || [];
      const first = msgs.find((message) => typeof message.seq === "number")?.seq;
      const seqs = new Set(msgs.map((message) => message.seq));
      const continuous = first !== undefined && first === previous.find((message) => typeof message.seq === "number")?.seq &&
        previous.every((message) => message.seq !== undefined && seqs.has(message.seq));
      return {
      messageWindows: { ...s.messageWindows, [sessionId]: continuous ? s.messageWindows[sessionId] : Symbol() },
      messages: { ...s.messages, [sessionId]: msgs.map(normalizeMessage) },
      closedQuestionIds: {
        ...s.closedQuestionIds,
        [sessionId]: {
          ...s.closedQuestionIds[sessionId],
          ...Object.fromEntries(msgs.filter((m) =>
            (m.type === "question_answer" || m.type === "question_closed") && m.tool_use_id
          ).map((m) => [m.tool_use_id!, true as const])),
        },
      },
      // A snapshot reload is authoritative; any half-streamed text it doesn't
      // contain is stale by definition.
      streamingText: preserveStreaming ? s.streamingText : dropKey(s.streamingText, sessionId),
      streamingItems: preserveStreaming || nativeBaseline ? { ...s.streamingItems, [sessionId]: Object.fromEntries(
        Object.entries(s.streamingItems[sessionId] || {}).filter(([key, item]) =>
          (preserveStreaming || item !== nativeBaseline?.[key]) &&
          !msgs.some((message) => message.type === "text" && nativeItemKey(message.runtime_metadata) === key)))
      } : dropKey(s.streamingItems, sessionId),
    }; }),

  oldestSeq: {},
  hasMoreMessages: {},
  messageWindows: {},
  setMessageWindow: (sessionId, oldestSeq, hasMore) =>
    set((s) => ({
      oldestSeq: { ...s.oldestSeq, [sessionId]: oldestSeq },
      hasMoreMessages: { ...s.hasMoreMessages, [sessionId]: hasMore },
    })),
  prependMessages: (sessionId, msgs, oldestSeq, hasMore, basis) => {
    let result: MessagePageResult = { state: "obsolete" };
    set((cur) => {
      if (cur.messageWindows[sessionId] !== basis.window || cur.oldestSeq[sessionId] !== basis.beforeSeq) return cur;
      if (msgs.some((message) => !Number.isInteger(message.seq) || message.seq! < 0) ||
          new Set(msgs.map((message) => message.seq)).size !== msgs.length) {
        result = { state: "failed", error: "Invalid history page sequence." }; return cur;
      }
      const fresh = msgs.filter((message) => message.seq! < basis.beforeSeq).sort((a, b) => a.seq! - b.seq!);
      if (fresh.length && oldestSeq !== null && oldestSeq !== fresh[0].seq) {
        result = { state: "failed", error: "History page cursor does not match its rows." }; return cur;
      }
      if (fresh.length === 0 && hasMore) {
        result = { state: "failed", error: "History page did not extend the window." }; return cur;
      }
      result = { state: "merged", added: fresh.length };
      return {
      messages: {
        ...cur.messages,
        [sessionId]: [...fresh.map(normalizeMessage), ...(cur.messages[sessionId] || [])],
      },
      oldestSeq: {
        ...cur.oldestSeq,
        [sessionId]: fresh[0]?.seq ?? cur.oldestSeq[sessionId],
      },
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
  appendStreamingText: (sessionId, chunk, metadata) =>
    set((s) => {
      const key = nativeItemKey(metadata);
      if (key && metadata) return { streamingItems: { ...s.streamingItems, [sessionId]: {
        ...s.streamingItems[sessionId], [key]: { runtime_metadata: metadata,
          content: (s.streamingItems[sessionId]?.[key]?.content || "") + chunk },
      } } };
      return {
      streamingText: {
        ...s.streamingText,
        [sessionId]: (s.streamingText[sessionId] || "") + chunk,
      },
      };
    }),
  clearStreamingText: (sessionId) =>
    set((s) =>
      s.streamingText[sessionId] === undefined && s.streamingItems[sessionId] === undefined
        ? s
        : { streamingText: dropKey(s.streamingText, sessionId), streamingItems: dropKey(s.streamingItems, sessionId) }
    ),

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
    set((s) => ({
      subagents: {
        ...s.subagents,
        [sessionId]: Object.fromEntries(
          mergeCardSnapshot(runs.filter((r) => r.card_id || r.tool_call_id), Object.values(s.subagents[sessionId] || {}),
            baseline === undefined ? undefined : Object.values(baseline), (r) => (r.card_id || r.tool_call_id)!,
            subagentUpdates, (r) => r.status === "running", advanceOnly)
            .map((r) => [(r.card_id || r.tool_call_id)!, r]).slice(-64)
        ),
      },
    })),

  schedules: [],
  scheduleRead: unreadList(),

  credentials: [],
  setCredentials: (credentials) => set({ credentials }),

  connectorCatalog: [],
  setConnectorCatalog: (connectorCatalog) => set({ connectorCatalog }),
  connectorInstallations: [],
  setConnectorInstallations: (connectorInstallations) =>
    set({ connectorInstallations }),
  upsertConnectorInstallation: (c) =>
    set((s) => {
      const idx = s.connectorInstallations.findIndex((i) => i.id === c.id);
      const connectorInstallations =
        idx >= 0
          ? [
              ...s.connectorInstallations.slice(0, idx),
              c,
              ...s.connectorInstallations.slice(idx + 1),
            ]
          : [...s.connectorInstallations, c];
      return { connectorInstallations };
    }),
  removeConnectorInstallation: (id) =>
    set((s) => ({
      connectorInstallations: s.connectorInstallations.filter(
        (i) => i.id !== id
      ),
    })),

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
  openViewer: (sessionId, path) => set({ viewer: { sessionId, path } }),
  closeViewer: () => set({ viewer: null }),

  bgTasks: {},
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
      const updated = mergeBgTask(task, current[idx], baseline, advanceOnly, baseline === undefined);
      const next = idx >= 0
        ? [...current.slice(0, idx), updated, ...current.slice(idx + 1)]
        : [...current, updated];
      return { bgTasks: { ...s.bgTasks, [sessionId]: next } };
    }),
  updateBgTaskDelivery: (sessionId, taskId, delivery) =>
    set((s) => {
      const current = s.bgTasks[sessionId] || [];
      const idx = current.findIndex((t) => t.id === taskId);
      if (idx < 0) return s;
      const updated = { ...current[idx], ...delivery };
      bgDeliveryUpdates.set(updated, Symbol());
      bgUpdates.set(updated, Symbol());
      return { bgTasks: { ...s.bgTasks, [sessionId]: [
        ...current.slice(0, idx), updated, ...current.slice(idx + 1),
      ] } };
    }),
  setBgTasks: (sessionId, tasks, baseline, advanceOnly = false, deliveryCurrent) =>
    set((s) => {
      const current = s.bgTasks[sessionId] || [];
      const previous = new Map(current.map((task) => [task.id, task]));
      const start = baseline && new Map(baseline.map((task) => [task.id, task]));
      const rows = tasks.map((task) => mergeBgTask(task, previous.get(task.id), start ? start.get(task.id) ?? null : undefined,
        advanceOnly || deliveryCurrent !== undefined && !deliveryCurrent(task.id)));
      if (start) {
        const incoming = new Set(tasks.map((task) => task.id));
        for (const task of current) {
          if (!incoming.has(task.id) && (!start.has(task.id) || advanceOnly ||
              bgUpdates.get(task) !== bgUpdates.get(start.get(task.id)!) ||
              start.get(task.id)?.status !== task.status)) rows.push(task);
        }
      }
      return { bgTasks: { ...s.bgTasks, [sessionId]: rows } };
    }),

  research: {},
  initializeResearch: (sessionId, job) => set((s) => {
    const current = s.research[sessionId] || [];
    const previous = current.find((item) => item.id === job.id);
    if (!previous) return { research: { ...s.research, [sessionId]: [...current, job] } };
    const updated = { ...previous };
    for (const field of ["question", "session_id", "created_at", "report_path"] as const) {
      if (updated[field] == null || updated[field] === "") Object.assign(updated, { [field]: job[field] });
    }
    const marker = researchUpdates.get(previous);
    if (marker) researchUpdates.set(updated, marker);
    return { research: { ...s.research, [sessionId]: current.map((item) => item === previous ? updated : item) } };
  }),
  confirmResearchSeen: (sessionId, jobId, receipt) => set((s) => {
    const current = s.research[sessionId] || [];
    const previous = current.find((item) => item.id === jobId);
    const before = previous?.delivery_receipt;
    if (!previous || !before || before.request_id !== receipt.request_id || before.version !== receipt.version ||
        receipt.browser_confirmed_version !== before.version) return s;
    const updated = { ...previous, delivery_receipt: { ...before,
      browser_confirmed_version: receipt.browser_confirmed_version, browser_confirmed_at: receipt.browser_confirmed_at } };
    const marker = researchUpdates.get(previous);
    if (marker) researchUpdates.set(updated, marker);
    return { research: { ...s.research, [sessionId]: current.map((item) => item === previous ? updated : item) } };
  }),
  upsertResearch: (sessionId, job) =>
    set((s) => {
      const current = s.research[sessionId] || [];
      const idx = current.findIndex((j) => j.id === job.id);
      if (idx < 0) {
        // Inserting a NEW card: only do so from a full payload (started /
        // snapshot). A bare progress/completed/failed patch for an unknown id
        // (missed `research_started` after reconnect or in a 2nd tab) would
        // render a card with no question/status — skip it; the /research
        // snapshot fetch on session load seeds those properly (Vera review).
        if (!job.question || !job.status) return {};
        const added = { ...job } as ResearchJob;
        researchUpdates.set(added, Symbol());
        return { research: { ...s.research, [sessionId]: [...current, added] } };
      }
      const updated = { ...current[idx], ...job };
      researchUpdates.set(updated, Symbol());
      const next = [
        ...current.slice(0, idx),
        updated,
        ...current.slice(idx + 1),
      ];
      return { research: { ...s.research, [sessionId]: next } };
    }),
  setResearch: (sessionId, jobs, baseline, advanceOnly) => set((s) => {
    const current = s.research[sessionId] || [];
    const rows = mergeAuxiliarySnapshot(jobs, current, baseline, researchUpdates, advanceOnly).map((item) => {
      const before = current.find((row) => row.id === item.id)?.delivery_receipt;
      const receipt = item.delivery_receipt;
      if (!receipt || before?.request_id !== receipt.request_id || !before.browser_confirmed_version ||
          before.browser_confirmed_version > receipt.version || before.browser_confirmed_version <= (receipt.browser_confirmed_version ?? 0)) return item;
      const updated = { ...item, delivery_receipt: { ...receipt,
        browser_confirmed_version: before.browser_confirmed_version, browser_confirmed_at: before.browser_confirmed_at } };
      const marker = researchUpdates.get(item);
      if (marker) researchUpdates.set(updated, marker);
      return updated;
    });
    return { research: { ...s.research, [sessionId]: rows } };
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
