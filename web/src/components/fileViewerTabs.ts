import { useRef, useState } from "react";
import type { FilePosition, FileResourceDirectory, FileViewerMetadata, ShowMeResolveResponse } from "../api";
import { isCurrentFileViewer, type FileViewerIntent, type FileViewerOptions } from "../stores/sessionStore";

type ReaderRequestOptions = FileViewerOptions & {
  // A retry reads the confirmed file. A later manual reload resolves its explicit alias again.
  reloadCommand?: string;
};

export type FileViewerRequest = ReaderRequestOptions & Pick<FileViewerIntent, "path" | "sessionId">;

export interface Opening {
  // Compare the user's original selection when settling earlier work.
  request: FileViewerRequest;
  readRequest?: FileViewerRequest;
  confirmed?: FileViewerMetadata;
  resolved?: Pick<ShowMeResolveResponse, "target" | "description">;
  failure?: OpenError;
  // A confirmed failure can be retried from the file slot or its originating tabs.
  failedRead?: { attempt: number; file: FileViewerMetadata };
  action: number;
  origin?: number;
  ignoreFailure?: true;
  // While unresolved, a newer failure settles this opening. Once confirmed, only matching files do.
  // An empty list records a newer failure with no confirmed file.
  supersededByFailures?: FileViewerMetadata[];
  // Scope suppression to files already settled by newer work.
  settledOnFile?: { outcome: "failure" | "ready"; files: FileViewerMetadata[] };
}

export interface OpenError {
  message: string;
  recovery?: "edit" | "retry";
  denied?: boolean;
  clarification?: true;
  originalPdf?: FileViewerMetadata;
}

export interface OpeningFailure extends Opening, OpenError {
  confirmedSource?: string;
}

export interface ReaderAttempt {
  id: number;
  attempt: number;
  // This request owns the panel and its read. Newer aliases can join it.
  request: FileViewerRequest;
  // The most recent selection comes first. A shared read settles all joined selections.
  openings: Opening[];
  phase: "opening" | "ready" | "recovery";
  meta?: FileViewerMetadata;
  position: FilePosition | null | undefined;
  positionAction: number;
  slow?: boolean;
}

export interface ReaderTab extends ReaderAttempt {
  failures?: OpeningFailure[];
  reload?: { name: string };
  retained?: ReaderAttempt;
}

export interface PickerLocation {
  source: string;
  directory: FileResourceDirectory;
}

interface ReaderPicker {
  action: number;
  origin?: number;
  location?: PickerLocation;
}

interface ReaderState {
  tabs: ReaderTab[];
  active: number | undefined;
  action: number;
  picker: ReaderPicker | undefined;
  reuseChecks: Opening[];
  readerFailure?: OpeningFailure;
  notice?: string;
}

const TAB_LIMIT = 8;

export function readerTabName(tab: ReaderTab) {
  if (tab.reload) return tab.reload.name;
  const path = tab.meta?.absolute_path ?? tab.openings[0].request.path;
  if (!tab.meta && tab.request.mode === "description") return "Find a file";
  return path.split("/").at(-1) || path || "Open file";
}

export function displayedReaderTab(tab: ReaderTab): ReaderAttempt {
  return tab.retained ?? tab;
}

export function selectableReaderTab(tab: ReaderTab) {
  return tab.phase !== "opening" || !!tab.reload;
}

export function usesFileResolver(request: FileViewerRequest) {
  return request.mode === "command" || request.mode === "description";
}

function liveOpenings(tab: ReaderAttempt, tabs: readonly ReaderTab[], pendingRetry = false) {
  return tab.openings.filter(({ origin, ignoreFailure, settledOnFile, confirmed }) =>
    !ignoreFailure && (!settledOnFile || (pendingRetry && settledOnFile.outcome === "ready" && !confirmed)) &&
    (origin === undefined || tabs.some((candidate) => candidate.id === origin)));
}

function openingPosition(opening: Opening) {
  const { position } = opening.readRequest ?? opening.request;
  return position === undefined ? opening.resolved?.target?.position ?? undefined : position;
}

export function confirmedOpeningFile(opening: Opening) {
  return opening.confirmed ?? opening.failedRead?.file;
}

export function displayedRecovery(tab: ReaderTab, tabs: readonly ReaderTab[]): ReaderTab {
  if (tab.phase !== "recovery") return tab;
  const openings = liveOpenings(tab, tabs);
  const [opening] = openings;
  if (!opening) return tab;
  const meta = confirmedOpeningFile(opening);
  if (opening === tab.openings[0] && meta === tab.meta) return tab;
  return { ...tab, openings, meta,
    ...(tab.reload || opening === tab.openings[0] ? {} : { position: openingPosition(opening), positionAction: opening.action }) };
}

export function openingDescription(opening: Opening) {
  const request = opening.readRequest ?? opening.request;
  return opening.resolved?.description ?? request.description ?? (request.mode === "description" ? request.path : undefined);
}

export function retryFileRead(opening: Opening): FileViewerRequest | undefined {
  const request = opening.readRequest ?? opening.request;
  const file = confirmedOpeningFile(opening);
  if (!usesFileResolver(request)) return { ...request, source: file?.source ?? request.source };
  // Retry the confirmed file without submitting another lookup command.
  if (!file) return;
  const description = openingDescription(opening);
  return {
    sessionId: request.sessionId, path: file.absolute_path, source: file.source,
    position: openingPosition(opening), description,
    reloadCommand: request.mode === "command" && !description ? request.path : undefined,
  };
}

export function pendingFileRetry(tab: ReaderAttempt, tabs: readonly ReaderTab[]) {
  const [opening] = liveOpenings(tab, tabs, true);
  if (!opening) return;
  const read = retryFileRead(opening);
  if (read) return { opening, read };
}

function sameFile(a: FileViewerMetadata, b: FileViewerMetadata) {
  return a.source === b.source && a.canonical_path_id === b.canonical_path_id;
}

function participatesInRead(opening: Opening, file: FileViewerMetadata | undefined) {
  const confirmed = confirmedOpeningFile(opening);
  return !file || !confirmed || sameFile(confirmed, file);
}

function confirmOpening(opening: Opening, file: FileViewerMetadata, resolved = opening.resolved): Opening {
  const settledOnFile = opening.settledOnFile?.files.some((target) => sameFile(target, file))
    ? opening.settledOnFile : undefined;
  const ignoreFailure = opening.supersededByFailures?.some((failed) => sameFile(failed, file)) ? true : opening.ignoreFailure;
  return { ...opening, confirmed: file, resolved, settledOnFile, ignoreFailure, supersededByFailures: undefined };
}

function canReportFailure(opening: Opening) {
  return !opening.ignoreFailure && !opening.settledOnFile && !opening.supersededByFailures;
}

function reusableMetadata(tab: ReaderTab) {
  if (tab.reload && !tab.retained) return;
  const { phase, meta } = displayedReaderTab(tab);
  return phase === "recovery" ? undefined : meta;
}

function matchesConfirmedFile(tab: ReaderTab, metadata: FileViewerMetadata) {
  const reusable = reusableMetadata(tab);
  return reusable !== undefined && sameFile(reusable, metadata);
}

function findReusableTab(tabs: ReaderTab[], metadata: FileViewerMetadata, excludedId?: number) {
  const matches = (tab: ReaderTab) => tab.id !== excludedId && matchesConfirmedFile(tab, metadata);
  return tabs.find((tab) => displayedReaderTab(tab).phase === "ready" && matches(tab)) ?? tabs.find(matches);
}

function sameRequestedFile(a: FileViewerRequest, b: FileViewerRequest) {
  return a.path === b.path && a.mode === b.mode && a.source === b.source &&
    a.sourceDirectory?.path === b.sourceDirectory?.path && a.sourceDirectory?.path_id === b.sourceDirectory?.path_id;
}

function samePendingTarget(a: Opening, b: Opening) {
  if (!sameRequestedFile(a.request, b.request)) return false;
  const fileA = confirmedOpeningFile(a);
  const fileB = confirmedOpeningFile(b);
  return fileA && fileB ? sameFile(fileA, fileB) : fileA === fileB;
}

function replacesFailure(previous: Opening, current: Opening) {
  if (current.action <= previous.action || !sameRequestedFile(previous.request, current.request)) return false;
  const previousFile = confirmedOpeningFile(previous);
  const currentFile = confirmedOpeningFile(current);
  return previousFile === undefined || (currentFile !== undefined && sameFile(previousFile, currentFile));
}

function confirmedRequest(tab: ReaderTab, request: FileViewerRequest) {
  const meta = reusableMetadata(tab);
  if (!meta || request.mode) return false;
  if (request.path === meta.absolute_path) return !request.source || request.source === meta.source;
  if (request.source !== meta.source) return false;
  const directory = request.sourceDirectory;
  return directory
    ? directory.path === meta.canonical_directory?.path && directory.path_id === meta.canonical_directory.path_id &&
      request.path === meta.path.split("/").at(-1)
    : request.path === meta.path;
}

function openingTab(id: number, attempt: number, opening: Opening): ReaderTab {
  const { action, request: selection, readRequest } = opening;
  const request = readRequest ?? selection;
  return {
    id, attempt, request, openings: [opening],
    phase: request.mode === "edit" ? "recovery" : "opening",
    position: request.position, positionAction: action,
  };
}

function reloadTab(tab: ReaderTab, attempt: number, opening: Opening): ReaderTab {
  const displayed = displayedReaderTab(tab);
  return {
    ...openingTab(tab.id, attempt, opening),
    reload: tab.reload ?? { name: readerTabName(tab) },
    retained: displayed.phase === "ready" ? displayed : undefined,
    failures: tab.failures,
  };
}

function recoveryTab(tab: ReaderTab, openings: Opening[]): ReaderTab {
  const [opening] = openings;
  return { ...tab, phase: "recovery", openings, slow: undefined, retained: undefined,
    position: tab.reload ? tab.position : openingPosition(opening),
    positionAction: tab.reload ? tab.positionAction : opening.action };
}

function replaceTab(tabs: ReaderTab[], tab: ReaderTab) {
  return tabs.map((other) => other.id === tab.id ? tab : other);
}

function removeTab(snapshot: ReaderState, tab: ReaderTab) {
  const index = snapshot.tabs.indexOf(tab);
  const tabs = snapshot.tabs.filter((other) => other !== tab);
  const active = snapshot.active === tab.id
    ? (tabs.slice(index).find((other) => displayedReaderTab(other).phase === "ready") ??
       tabs.slice(0, index).reverse().find((other) => displayedReaderTab(other).phase === "ready"))?.id
    : snapshot.active;
  return { ...snapshot, tabs, active };
}

function discardTab(snapshot: ReaderState, tab: ReaderTab): ReaderState {
  return { ...removeTab(snapshot, tab),
    reuseChecks: snapshot.reuseChecks.filter((check) => check.origin !== tab.id),
    picker: snapshot.picker?.origin === tab.id ? { ...snapshot.picker, origin: undefined } : snapshot.picker };
}

function discardEmptyRecoveries(snapshot: ReaderState): ReaderState {
  let next = snapshot;
  while (true) {
    const empty = next.tabs.find((tab) => tab.phase === "recovery" && !liveOpenings(tab, next.tabs).length);
    if (!empty) return next;
    next = discardTab(next, empty);
  }
}

function reportOpeningFailure(snapshot: ReaderState, origin: ReaderTab | undefined, opening: Opening, error: OpenError,
  confirmedSource?: string) {
  const failure = { ...opening, message: error.message, recovery: error.recovery, originalPdf: error.originalPdf, confirmedSource };
  if (!origin) return { ...snapshot, readerFailure: failure };
  if (origin.failures?.some((existing) => replacesFailure(opening, existing))) {
    return snapshot;
  }
  const failures = (origin.failures ?? []).filter((failure) => !replacesFailure(failure, opening));
  return { ...snapshot, tabs: replaceTab(snapshot.tabs, { ...origin, failures: [...failures, failure] }) };
}

export function failedFileRead(failure: OpeningFailure): FileViewerRequest {
  const request = failure.readRequest ?? failure.request;
  return { ...request, source: failure.confirmedSource ?? request.source };
}

function joinOpenings(current: Opening[], incoming: Opening[]) {
  const joined: Opening[] = [];
  for (const opening of [...current, ...incoming].sort((a, b) => b.action - a.action)) {
    // A later selection still shares the read with older recovery groups.
    if (!joined.some((other) => other.origin === opening.origin && samePendingTarget(other, opening) &&
      other.failedRead?.attempt === opening.failedRead?.attempt)) {
      joined.push(opening);
    }
  }
  return joined;
}

function settleOpenings(snapshot: ReaderState, openings: Opening[], outcome: "ready" | "failure"): ReaderState {
  const settled = openings.filter((opening) => opening.origin && !opening.ignoreFailure && !opening.settledOnFile &&
    (outcome === "ready" || canReportFailure(opening)));
  if (!settled.length) return snapshot;
  const laterFromOrigin = (newer: Opening, older: Opening) => newer.origin === older.origin && newer.action > older.action;
  const replacesAtOrigin = (older: Opening, newer: Opening) => newer.origin === older.origin && replacesFailure(older, newer);
  const supersededTarget = (older: Opening) => settled.some((opening) => replacesAtOrigin(older, opening));
  const supersededSelection = (older: Opening) => supersededTarget(older) || settled.some((opening) =>
    laterFromOrigin(opening, older) && sameRequestedFile(opening.request, older.request) &&
    confirmedOpeningFile(opening) && (!older.failedRead || participatesInRead(older, older.failedRead.file)));
  const suppress = (opening: Opening): Opening => {
    if (opening.failedRead && participatesInRead(opening, opening.failedRead.file)) {
      return { ...opening, settledOnFile: opening.settledOnFile ??
        { outcome: "failure", files: [opening.failedRead.file] } };
    }
    if (outcome === "failure" && !confirmedOpeningFile(opening) && !opening.ignoreFailure) {
      const files = [...(opening.supersededByFailures ?? [])];
      for (const newer of settled.filter((selection) => replacesAtOrigin(opening, selection))) {
        const file = confirmedOpeningFile(newer);
        if (file && !files.some((failed) => sameFile(failed, file))) files.push(file);
      }
      return { ...opening, supersededByFailures: files };
    }
    return { ...opening, ignoreFailure: true, supersededByFailures: undefined };
  };
  // Cancellation does not settle an opening.
  const tabs = snapshot.tabs.map((tab): ReaderTab => {
    let updated = tab;
    if (tab.failures?.some(supersededTarget)) {
      updated = { ...updated, failures: tab.failures.filter((failure) => !supersededTarget(failure)) };
    }
    if (tab.phase !== "ready") {
      const file = tab.meta;
      // Retries can carry recovery history for a different file.
      const members = file ? tab.openings.filter((opening) => {
        const target = confirmedOpeningFile(opening);
        return target !== undefined && sameFile(target, file);
      }) : [];
      const superseded = (opening: Opening) => {
        if (outcome === "failure") return supersededTarget(opening);
        if (tab.phase === "recovery") return supersededSelection(opening);
        // A still-unresolved read is settled by file identity once it confirms.
        if (!confirmedOpeningFile(opening)) return false;
        // An ordinary selection in a shared read may be replaced without retiring the read.
        const sharedSelection = members.length > 1 && members.includes(opening) && !opening.failedRead;
        return sharedSelection ? supersededSelection(opening) : supersededTarget(opening);
      };
      if (tab.openings.some(superseded)) {
        updated = { ...updated, openings: tab.openings.map((opening) => superseded(opening) ? suppress(opening) : opening) };
      }
    }
    return updated;
  });
  const reuseChecks = snapshot.reuseChecks.flatMap((check): Opening[] => {
    if (!supersededTarget(check)) return [check];
    const suppressed = suppress(check);
    // A ready replacement retires ordinary confirmations. Shared retries still need to identify the file.
    return check.failedRead || (outcome === "failure" && suppressed.supersededByFailures) ? [suppressed] : [];
  });
  return { ...snapshot, tabs, reuseChecks };
}

function settleReadyRead(snapshot: ReaderState, ready: Partial<Pick<ReaderAttempt, "id" | "meta">>, openings: Opening[]): ReaderState {
  const file = ready.meta;
  const participants = openings.filter((opening) => participatesInRead(opening, file));
  const settled = settleOpenings(snapshot, participants, "ready");
  if (!file) return settled;
  const recovered = participants.filter(({ failedRead, ignoreFailure }) =>
    failedRead && !ignoreFailure && sameFile(failedRead.file, file));
  const predatesRecovery = (opening: Opening) => recovered.some((success) =>
    success.failedRead?.attempt === opening.failedRead?.attempt && success.action > opening.action);
  const settleConfirmation = (opening: Opening): Opening => {
    if (!participatesInRead(opening, file)) return opening;
    const files = opening.settledOnFile?.outcome === "ready" ? opening.settledOnFile.files : [];
    if (files.some((target) => sameFile(target, file))) return opening;
    // A usable read settles older confirmations for its file even when its own failure would have been stale.
    const superseded = opening.origin !== undefined && participants.some((success) =>
      success.origin === opening.origin && success.action > opening.action && sameRequestedFile(opening.request, success.request));
    return superseded || predatesRecovery(opening)
      ? { ...opening, settledOnFile: { outcome: "ready", files: [...files, file] } } : opening;
  };
  const tabs = settled.tabs.map((tab): ReaderTab => {
    const failures = tab.failures?.filter((failure) => !predatesRecovery(failure));
    if (tab.phase === "ready") return failures?.length === tab.failures?.length ? tab : { ...tab, failures };
    const openings = tab.openings.map(settleConfirmation);
    return openings.every((opening, index) => opening === tab.openings[index]) && failures?.length === tab.failures?.length
      ? tab : { ...tab, openings, failures };
  });
  const reuseChecks = settled.reuseChecks.map(settleConfirmation);
  if (ready.id === undefined) return { ...settled, tabs, reuseChecks };
  const retired = tabs.filter((tab) => tab.phase === "recovery" && tab.openings.some(predatesRecovery) &&
    !liveOpenings(tab, tabs).length);
  const wasRetired = (id: number | undefined) => retired.some((tab) => tab.id === id);
  return {
    ...settled,
    tabs: tabs.filter((tab) => !wasRetired(tab.id)),
    active: wasRetired(settled.active) ? ready.id : settled.active,
    picker: settled.picker && wasRetired(settled.picker.origin) ? { ...settled.picker, origin: ready.id } : settled.picker,
    reuseChecks: reuseChecks.map((check) => wasRetired(check.origin)
      ? { ...check, origin: ready.id } : check),
  };
}

function settleLateConfirmation(snapshot: ReaderState, openings: Opening[], file: FileViewerMetadata) {
  const recoveryMembers = openings.filter(({ failedRead }) => failedRead && sameFile(failedRead.file, file));
  if (!recoveryMembers.length) return snapshot;
  // The old read is retired, but its verified file still settles its recovery group.
  const ready = snapshot.tabs.find((tab) => displayedReaderTab(tab).phase === "ready" && matchesConfirmedFile(tab, file));
  return settleReadyRead(snapshot, { id: ready?.id, meta: file }, recoveryMembers);
}

function merge(snapshot: ReaderState, survivor: ReaderTab, incoming: Opening[], duplicate?: ReaderTab) {
  const tab = { ...displayedReaderTab(survivor) };
  const replacesActive = duplicate !== undefined && snapshot.active === duplicate.id;
  const joined = joinOpenings(tab.openings, incoming);
  const [latest] = joined;
  tab.openings = tab.phase === "ready" ? [latest] : joined;
  const incomingOpening = incoming[0];
  const position = duplicate ? duplicate.position : (incomingOpening.readRequest ?? incomingOpening.request).position;
  const positionAction = duplicate?.positionAction ?? incomingOpening.action;
  const nextPosition = position !== undefined && positionAction > tab.positionAction
    ? position === null ? null : { ...position } : undefined;
  if (nextPosition !== undefined) {
    tab.position = nextPosition;
    tab.positionAction = positionAction;
  }
  const updated = survivor.retained ? { ...survivor, retained: tab,
    ...(nextPosition !== undefined && positionAction > survivor.positionAction ? { position: nextPosition, positionAction } : {}),
  } : { ...survivor, ...tab };
  const activate = incomingOpening.action === snapshot.action && tab.phase === "ready";
  const tabs = duplicate ? snapshot.tabs.filter((other) => other !== duplicate) : snapshot.tabs;
  const merged = { ...snapshot,
    tabs: replaceTab(tabs, updated),
    active: activate || replacesActive ? tab.id : snapshot.active,
    // The surviving slot inherits picker actions without restarting their reads.
    picker: duplicate && snapshot.picker?.origin === duplicate.id
      ? { ...snapshot.picker, origin: survivor.id } : snapshot.picker,
    reuseChecks: duplicate ? snapshot.reuseChecks.map((check) => check.origin === duplicate.id
      ? { ...check, origin: survivor.id } : check) : snapshot.reuseChecks,
  };
  return tab.phase === "ready" ? settleReadyRead(merged, tab, incoming) : merged;
}

export function useFileViewerTabs(intent: FileViewerIntent) {
  const [state, setState] = useState<ReaderState>(() => ({
    tabs: intent.mode === "picker" ? [] : [openingTab(1, 1, { request: intent, action: 1 })],
    active: intent.mode === "picker" ? undefined : 1,
    action: 1,
    picker: intent.mode === "picker" ? { action: 1 } : undefined,
    reuseChecks: [],
  }));
  // A response and a click can arrive before React renders either. Operations
  // read the latest state here and publish one snapshot to the UI.
  const current = useRef(state);
  const [actions] = useState(() => {
    let lastTabId = 1;
    let lastAttempt = 1;
    const commit = (next: ReaderState) => {
      const committed = discardEmptyRecoveries(next);
      current.current = committed;
      setState(committed);
    };
    const owned = (request: FileViewerRequest) => isCurrentFileViewer(intent)
      ? current.current.tabs.find((tab) => tab.request === request) : undefined;
    const activeTab = (request: FileViewerRequest) => {
      if (!isCurrentFileViewer(intent) || current.current.picker) return;
      return current.current.tabs.find((tab) => current.current.active === tab.id &&
        displayedReaderTab(tab).request === request);
    };
    const canInteract = (request: FileViewerRequest) => activeTab(request) !== undefined;
    const canEditPath = (request: FileViewerRequest) => {
      const tab = activeTab(request);
      return tab !== undefined && !tab.retained;
    };
    const start = (path: string, options: ReaderRequestOptions,
      entry: { origin: FileViewerRequest; reload?: boolean; failure?: OpeningFailure } |
        { picker: number } | { readerFailure: OpeningFailure }, originalRequest?: FileViewerRequest) => {
      if (!isCurrentFileViewer(intent)) return;
      const snapshot = current.current;
      const fromTab = "origin" in entry;
      const fromPicker = "picker" in entry;
      const originTab = fromTab ? activeTab(entry.origin)
        : fromPicker ? snapshot.tabs.find((tab) => tab.id === snapshot.picker?.origin) : undefined;
      if (fromTab && !originTab) return;
      if (fromPicker && snapshot.picker?.action !== entry.picker) return;
      if ("readerFailure" in entry && (snapshot.picker || snapshot.readerFailure !== entry.readerFailure)) return;
      const recoveryOpenings = originTab?.phase === "recovery" && fromTab && entry.reload
        ? liveOpenings(originTab, snapshot.tabs) : [];
      const [recovery] = recoveryOpenings;
      const previous = recovery ?? (originalRequest && fromTab ? entry.failure : undefined);
      const request = { ...options, sessionId: intent.sessionId, path };
      const selection = recovery?.request ?? originalRequest ?? request;
      const action = snapshot.action + 1;
      const reloading = fromTab && entry.reload && originTab &&
        (originTab.reload || displayedReaderTab(originTab).phase === "ready");
      const replacing = originTab && ((fromTab && entry.reload) || originTab.phase === "recovery");
      const couldReuse = !replacing && snapshot.tabs.some((tab) => confirmedRequest(tab, request));
      if (!replacing && !couldReuse && snapshot.tabs.length >= TAB_LIMIT) {
        commit({ ...snapshot, picker: undefined, action, notice: "Close a file tab or cancel an opening to make room." });
        return;
      }
      const origin = recovery?.origin ?? (!replacing && originTab && displayedReaderTab(originTab).phase === "ready"
        ? originTab.id : undefined);
      const opening: Opening = { request: selection, readRequest: recovery || originalRequest ? request : undefined,
        action, origin, failedRead: previous?.failedRead };
      const retriedFailure = fromTab ? entry.failure : undefined;
      const cleanOrigin = originTab?.failures && retriedFailure ? {
        ...originTab, failures: originTab.failures.filter((failure) => failure !== retriedFailure),
      } : undefined;
      const tabs = cleanOrigin ? replaceTab(snapshot.tabs, cleanOrigin) : snapshot.tabs;
      const next = { ...snapshot, tabs, action, picker: undefined, notice: undefined, readerFailure: undefined };
      if (couldReuse) {
        // A known target can share a slot after confirming current access.
        const check = { ...opening, origin: originTab?.id };
        const checks = next.reuseChecks.filter((other) =>
          other.origin !== check.origin || !samePendingTarget(other, check));
        commit({ ...next, reuseChecks: [...checks, check] });
        return;
      }
      const attempt = ++lastAttempt;
      const tab = reloading ? reloadTab(cleanOrigin ?? originTab, attempt, opening)
        : openingTab(replacing ? originTab.id : ++lastTabId, attempt, opening);
      if (recovery) tab.openings = joinOpenings([opening], recoveryOpenings);
      commit({
        ...next, tabs: replacing ? replaceTab(tabs, tab) : [...tabs, tab],
        active: replacing || !tabs.length ? tab.id : snapshot.active,
      });
    };
    return {
      owns: (request: FileViewerRequest) => !!owned(request),
      canInteract,
      canEditPath,
      open(origin: FileViewerRequest, path: string, options: FileViewerOptions = {}) {
        start(path, options, { origin });
      },
      edit(origin: FileViewerRequest, path: string, options: FileViewerOptions, samePath: boolean) {
        if (!canEditPath(origin)) return;
        start(path, options, { origin, reload: samePath });
      },
      reload(origin: FileViewerRequest, path: string, options: ReaderRequestOptions) {
        start(path, options, { origin, reload: true });
      },
      pick(picker: number, path: string, options: FileViewerOptions) {
        start(path, options, { picker });
      },
      retryReaderFailure(failure: OpeningFailure) {
        const read = failedFileRead(failure);
        start(read.path, read, { readerFailure: failure }, failure.request);
      },
      retryReuse(previousAction: number) {
        const snapshot = current.current;
        if (!isCurrentFileViewer(intent) || snapshot.picker) return;
        const check = snapshot.reuseChecks.find((item) => item.action === previousAction);
        if (!check) return;
        const action = snapshot.action + 1;
        const reuseChecks = snapshot.reuseChecks.map((item) => item === check
          ? { ...check, action, ignoreFailure: undefined, settledOnFile: undefined, supersededByFailures: undefined } : item);
        commit({ ...snapshot, action, reuseChecks });
      },
      settleReuse(action: number, result?: { meta: FileViewerMetadata } | { error: OpenError }) {
        const snapshot = current.current;
        if (!isCurrentFileViewer(intent)) return;
        const check = snapshot.reuseChecks.find((item) => item.action === action);
        if (!check) return;
        const next = { ...snapshot, reuseChecks: snapshot.reuseChecks.filter((other) => other !== check) };
        const origin = next.tabs.find((tab) => tab.id === check.origin);
        if (!result || (check.origin !== undefined && !origin)) {
          commit(next);
          return;
        }
        if ("error" in result) {
          const report = canReportFailure(check) && (origin || next.action === action);
          commit(settleOpenings(report ? reportOpeningFailure(next, origin, check, result.error) : next, [check], "failure"));
          return;
        }
        const confirmed = confirmOpening(check, result.meta);
        if (confirmed.settledOnFile?.outcome === "ready") {
          commit(settleLateConfirmation(next, [confirmed], result.meta));
          return;
        }
        const target = findReusableTab(next.tabs, result.meta);
        if (target) {
          commit(merge(next, target, [confirmed]));
          return;
        }
        const recovery = origin?.phase === "recovery" && next.active === origin.id && next.action === check.action;
        if (!recovery && next.tabs.length >= TAB_LIMIT) {
          commit(next.action === check.action ? { ...next, notice: "Close a file tab or cancel an opening to make room." } : next);
          return;
        }
        const tab = { ...openingTab(recovery ? origin.id : ++lastTabId, ++lastAttempt,
          { ...confirmed, origin: recovery ? undefined : origin?.id }),
          meta: result.meta };
        commit({ ...next, tabs: recovery ? replaceTab(next.tabs, tab) : [...next.tabs, tab],
          active: next.tabs.length === 0 ? tab.id : next.active });
      },
      confirm(request: FileViewerRequest, meta: FileViewerMetadata, resolved?: Opening["resolved"]) {
        const tab = owned(request);
        if (tab?.phase !== "opening") return;
        const snapshot = current.current;
        const ownsRead = (opening: Opening) => (opening.readRequest ?? opening.request) === request;
        const openings = tab.openings.map((opening) => ownsRead(opening)
          ? confirmOpening(opening, meta, resolved) : opening);
        const confirmed = { ...tab, meta, openings,
          position: tab.position === undefined ? resolved?.target?.position ?? undefined : tab.position };
        if (openings.some((opening) => ownsRead(opening) && opening.settledOnFile?.outcome === "ready")) {
          const next = settleLateConfirmation({ ...snapshot, tabs: replaceTab(snapshot.tabs, confirmed) }, openings, meta);
          const pending = next.tabs.find((other) => other.id === tab.id) ?? confirmed;
          const fallback = liveOpenings(pending, next.tabs).filter(({ failure }) => failure !== undefined);
          commit(fallback.length ? { ...next, tabs: replaceTab(next.tabs, recoveryTab(pending, fallback)) }
            : discardTab(next, pending));
          return;
        }
        const match = !tab.reload && findReusableTab(snapshot.tabs, meta, tab.id);
        const next = { ...snapshot, tabs: replaceTab(snapshot.tabs, confirmed) };
        if (match) {
          commit(merge(next, match, confirmed.openings, confirmed));
          return;
        }
        commit(next);
      },
      ready(request: FileViewerRequest) {
        const tab = owned(request);
        if (tab?.phase !== "opening") return;
        const snapshot = current.current;
        const confirmed = tab.meta;
        const existing = confirmed && snapshot.tabs.find((other) => other.id !== tab.id &&
          displayedReaderTab(other).phase === "ready" && matchesConfirmedFile(other, confirmed));
        if (existing) {
          if (tab.reload) return "This file is already open in another tab.";
          commit(merge(snapshot, existing, tab.openings, tab));
          return;
        }
        const [opening] = tab.openings;
        const ready = { ...tab, phase: "ready" as const, openings: [opening], slow: undefined, reload: undefined, retained: undefined };
        commit(settleReadyRead({ ...snapshot, tabs: replaceTab(snapshot.tabs, ready),
          active: !tab.reload && opening.action === snapshot.action ? tab.id : snapshot.active }, ready, tab.openings));
      },
      fail(request: FileViewerRequest, error: OpenError) {
        const tab = owned(request);
        if (tab?.phase !== "opening") return;
        const snapshot = current.current;
        const openings = liveOpenings(tab, snapshot.tabs).filter(canReportFailure);
        const [opening] = openings;
        const reading = tab.openings.find((entry) => (entry.readRequest ?? entry.request) === request);
        const file = tab.meta ?? reading?.failedRead?.file;
        const sharesFailedFile = (entry: Opening) => participatesInRead(entry, file);
        const previousFailure = openings.find(({ failedRead }) => failedRead && (!file || sameFile(failedRead.file, file)))?.failedRead;
        const failedRead = previousFailure ?? (file ? { attempt: tab.attempt, file } : undefined);
        // Any live opening without a separate origin can keep this slot for recovery.
        const recoverable = opening && openings.some(({ origin }) => origin === undefined || origin === tab.id);
        let next = snapshot;
        if (recoverable) {
          const recovery = recoveryTab(tab, openings.map((entry) => sharesFailedFile(entry)
            ? { ...entry, failedRead, failure: error } : entry));
          const showRecovery = !tab.reload && (opening.action === snapshot.action || opening !== tab.openings[0]);
          next = { ...snapshot, tabs: replaceTab(snapshot.tabs, recovery),
            active: snapshot.active ?? (showRecovery ? tab.id : undefined) };
        } else {
          next = removeTab(snapshot, tab);
        }
        for (const origin of next.tabs) {
          if (origin.id === tab.id) continue;
          const selection = openings.find((entry) => entry.origin === origin.id && sharesFailedFile(entry));
          if (selection) {
            next = reportOpeningFailure(next, origin, { ...selection, failedRead,
              readRequest: retryFileRead(selection) ?? selection.readRequest }, error, file?.source);
          }
        }
        commit(settleOpenings(next, tab.openings.filter(sharesFailedFile), "failure"));
      },
      reopenFailure(origin: FileViewerRequest, failure: OpeningFailure,
        edit?: { path: string; options: ReaderRequestOptions; samePath: boolean }) {
        if (!activeTab(origin)?.failures?.includes(failure)) return;
        const read = failedFileRead(failure);
        start(edit?.path ?? read.path, edit?.options ?? read, { origin, failure },
          !edit || edit.samePath ? failure.request : undefined);
      },
      markSlow(request: FileViewerRequest) {
        const tab = owned(request);
        if (tab?.phase !== "opening" || tab.slow) return;
        const snapshot = current.current;
        commit({ ...snapshot, tabs: replaceTab(snapshot.tabs, { ...tab, slow: true }) });
      },
      retryPending(request: FileViewerRequest) {
        const tab = owned(request);
        const snapshot = current.current;
        if (tab?.phase !== "opening" || snapshot.picker || (!tab.slow && !canInteract(request))) return;
        const retry = pendingFileRetry(tab, snapshot.tabs);
        if (!retry) return;
        const { opening: previous, read } = retry;
        const readRequest = { ...read };
        const attempt = ++lastAttempt;
        if (tab.reload) {
          // Restart this read without changing any tab's foreground action.
          const next = reloadTab(tab, attempt, { ...previous, readRequest });
          commit({ ...snapshot, tabs: replaceTab(snapshot.tabs, {
            ...next, position: tab.position, positionAction: tab.positionAction,
          }) });
          return;
        }
        const action = snapshot.action + 1;
        // A shared slot can be reused by an alias opened from another tab.
        // The selection being retried determines where its failure belongs.
        const origin = previous.origin === tab.id ? undefined : previous.origin;
        const opening = { origin, action, request: previous.request, readRequest, failedRead: previous.failedRead };
        const next = openingTab(tab.id, attempt, opening);
        const members = previous.failedRead ? tab.openings : tab.openings.filter((entry) => entry.failedRead);
        if (members.length) next.openings = joinOpenings([opening], members);
        commit({ ...snapshot, action, tabs: replaceTab(snapshot.tabs, next) });
      },
      select(request: FileViewerRequest) {
        const tab = owned(request);
        const snapshot = current.current;
        if (!tab || snapshot.picker || (!selectableReaderTab(tab) && snapshot.active !== tab.id)) return;
        commit({ ...snapshot, active: tab.id, action: snapshot.action + 1 });
      },
      locate(request: FileViewerRequest, position?: FilePosition | null) {
        const tab = activeTab(request);
        if (!tab) return false;
        const snapshot = current.current;
        const action = snapshot.action + 1;
        const selection = position === undefined ? {} : { position };
        const located = tab.retained ? {
          ...tab, retained: { ...tab.retained, positionAction: action, ...selection },
          ...(position === undefined ? {} : { position, positionAction: action }),
        } : { ...tab, positionAction: action, ...selection };
        commit({ ...snapshot, action, tabs: replaceTab(snapshot.tabs, located) });
        return true;
      },
      close(request: FileViewerRequest) {
        const tab = owned(request);
        if (!tab) return;
        const snapshot = current.current;
        commit({ ...discardTab(snapshot, tab), notice: undefined });
      },
      showPicker(origin?: FileViewerRequest) {
        if (!isCurrentFileViewer(intent) || (origin && !canInteract(origin))) return;
        const snapshot = current.current;
        if (snapshot.picker) return;
        const tab = snapshot.tabs.find((tab) => tab.id === snapshot.active);
        const displayed = tab && displayedReaderTab(tab);
        const meta = displayed?.phase === "ready" ? displayed.meta : undefined;
        const location = meta?.canonical_directory ? { source: meta.source, directory: meta.canonical_directory } : undefined;
        const action = snapshot.action + 1;
        commit({ ...snapshot, picker: { action, origin: tab?.id, location }, action, notice: undefined });
      },
      hidePicker(picker: number) {
        if (!isCurrentFileViewer(intent) || current.current.picker?.action !== picker) return;
        commit({ ...current.current, picker: undefined });
      },
    };
  });
  return { state, actions };
}

export type FileViewerTabActions = ReturnType<typeof useFileViewerTabs>["actions"];
