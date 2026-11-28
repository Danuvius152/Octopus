import { useRef, useState } from "react";
import type { FilePosition, FileResourceDirectory, FileViewerMetadata, ShowMeFileResponse } from "../api";
import { isCurrentFileViewer, type FileViewerIntent, type FileViewerOptions } from "../stores/sessionStore";

export type FileViewerRequest = FileViewerOptions & Pick<FileViewerIntent, "path" | "sessionId"> & { reloadSource?: string };
type ConfirmedTarget = Pick<FileViewerMetadata, "absolute_path" | "source">;

export interface Opening {
  request: FileViewerRequest;
  action: number;
  origin?: number;
  feedbackId?: number;
  confirmed?: FileViewerMetadata;
  // Available for path editing while a fresh confirmation has no readable snapshot.
  previousTarget?: ConfirmedTarget;
  resolved?: Pick<ShowMeFileResponse, "target" | "description">;
}

export interface OpenError {
  message: string;
  recovery?: "edit" | "retry" | "reconfirm" | "reopen-source";
  denied?: boolean;
  clarification?: true;
  originalPdf?: FileViewerMetadata;
}

export type OpeningFailure = Opening & OpenError;

export interface PositionCheck {
  position: FilePosition;
  action: number;
}

export interface ReaderAttempt {
  id: number;
  attempt: number;
  // Each retry gets a new request object. Its Opening keeps the user's original command.
  request: FileViewerRequest;
  opening: Opening;
  phase: "opening" | "ready" | "recovery";
  position: FilePosition | null | undefined;
  positionCheck?: PositionCheck;
  positionReport?: number;
  invalidLocation?: string;
  error?: OpenError;
  slow?: boolean;
}

export interface ReaderTab extends ReaderAttempt {
  initial?: true;
  feedback?: { id: number; failure?: OpeningFailure };
  positionNotice?: { id: string; message: string };
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

interface ReuseCheck extends Opening {
  attempt: number;
  confirmationRequest: FileViewerRequest;
}

interface ReaderState {
  tabs: ReaderTab[];
  active: number | undefined;
  action: number;
  picker: ReaderPicker | undefined;
  reuseChecks: ReuseCheck[];
  readerFailure?: OpeningFailure;
  notice?: string;
}

const TAB_LIMIT = 8;
const NO_SLOT = "Close a file tab or cancel an opening to make room.";

export function readerTabName(tab: ReaderTab) {
  if (tab.reload) return tab.reload.name;
  const path = tab.opening.confirmed?.absolute_path ?? tab.request.path;
  if (!tab.opening.confirmed && tab.request.mode === "description") return "Find a file";
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

export function canRetryPendingFile(tab: ReaderAttempt) {
  return tab.phase === "opening" && (!!tab.opening.confirmed || !usesFileResolver(tab.request));
}

export function failedFileRead(opening: Opening): FileViewerRequest {
  const { request, confirmed, previousTarget, resolved } = opening;
  return {
    ...request, source: (confirmed ?? previousTarget)?.source ?? request.source,
    position: request.position === undefined ? resolved?.target.position ?? undefined : request.position,
    description: resolved?.description ?? request.description,
  };
}

function matchesReadyFile(tab: ReaderTab, file: FileViewerMetadata) {
  const displayed = displayedReaderTab(tab);
  const ready = displayed.opening.confirmed;
  return displayed.phase === "ready" && ready?.source === file.source && ready.canonical_path_id === file.canonical_path_id;
}

function knownReadyTarget(tab: ReaderTab, request: FileViewerRequest) {
  const displayed = displayedReaderTab(tab);
  const meta = displayed.opening.confirmed;
  if (displayed.phase !== "ready" || !meta || request.mode) return false;
  if (request.path === meta.absolute_path) return !request.source || request.source === meta.source;
  if (request.source !== meta.source) return false;
  const directory = request.sourceDirectory;
  return directory
    ? directory.path === meta.canonical_directory?.path && directory.path_id === meta.canonical_directory.path_id &&
      request.path === meta.path.split("/").at(-1)
    : request.path === meta.path;
}

function openingTab(id: number, attempt: number, opening: Opening, request = opening.request): ReaderTab {
  return {
    id, attempt, opening, request: { ...request },
    phase: request.mode === "edit" ? "recovery" : "opening",
    position: opening.request.position === undefined ? opening.resolved?.target.position ?? undefined : opening.request.position,
    positionReport: opening.action,
  };
}

function reloadTab(tab: ReaderTab, next: ReaderTab): ReaderTab {
  const displayed = displayedReaderTab(tab);
  return {
    ...next,
    reload: tab.reload ?? { name: readerTabName(tab) },
    retained: displayed.phase === "ready" ? displayed : undefined,
  };
}

function replaceTab(snapshot: ReaderState, tab: ReaderTab): ReaderState {
  return { ...snapshot, tabs: snapshot.tabs.map((other) => other.id === tab.id ? tab : other) };
}

function appendTab(snapshot: ReaderState, tab: ReaderTab): ReaderState {
  if (snapshot.tabs.length) return { ...snapshot, tabs: [...snapshot.tabs, tab] };
  return { ...snapshot, tabs: [{ ...tab, initial: true }], active: tab.id };
}

function removeTab(snapshot: ReaderState, tab: ReaderTab): ReaderState {
  const index = snapshot.tabs.indexOf(tab);
  const tabs = snapshot.tabs.filter((other) => other !== tab);
  const ready = (other: ReaderTab) => displayedReaderTab(other).phase === "ready";
  const active = snapshot.active === tab.id
    ? (tabs.slice(index).find(ready) ?? tabs.slice(0, index).reverse().find(ready))?.id
    : snapshot.active;
  return { ...snapshot, tabs, active,
    picker: snapshot.picker?.origin === tab.id ? { ...snapshot.picker, origin: undefined } : snapshot.picker };
}

function positionTab(tab: ReaderTab, position: FilePosition | null, action: number): ReaderTab {
  // Reopening the same link still owns a fresh scroll on the existing snapshot.
  return updateTabLocation(tab, { position: position && { ...position }, positionCheck: undefined,
    positionReport: action, invalidLocation: undefined });
}

type LocationUpdate = Partial<Pick<ReaderAttempt, "position" | "positionCheck" | "positionReport" | "invalidLocation">>;

function updateTabLocation(tab: ReaderTab, location: LocationUpdate): ReaderTab {
  return { ...tab, ...location, retained: tab.retained && { ...tab.retained, ...location } };
}

function updateDisplayedLocation(tab: ReaderTab, location: LocationUpdate): ReaderTab {
  return tab.retained ? { ...tab, retained: { ...tab.retained, ...location } } : { ...tab, ...location };
}

function clearReadingFeedback(tab: ReaderTab, keepReloadPosition = false): ReaderTab {
  const cleared = { ...tab, feedback: undefined, positionNotice: undefined };
  const reset = { positionCheck: undefined, positionReport: undefined };
  // A new location on the retained body may be invalid. Its pending replacement
  // still validates the last applied position unless the new location succeeds.
  return keepReloadPosition ? updateDisplayedLocation(cleared, reset) : updateTabLocation(cleared, reset);
}

function showPositionNotice(tab: ReaderTab, action: number, message: string): ReaderTab {
  return { ...tab, positionNotice: { id: `${displayedReaderTab(tab).attempt}:${action}`, message } };
}

function activateTab(snapshot: ReaderState, id: number) {
  const former = snapshot.tabs.find((tab) => tab.id === snapshot.active && tab.id !== id);
  return { ...(former ? replaceTab(snapshot, clearReadingFeedback(former)) : snapshot), active: id };
}

function reportOpeningFailure(snapshot: ReaderState, opening: Opening, error: OpenError): ReaderState {
  const { message, recovery, denied, clarification, originalPdf } = error;
  const failure = { ...opening, message, recovery, denied, clarification, originalPdf };
  if (opening.origin === undefined) {
    return opening.action === snapshot.action ? { ...snapshot, readerFailure: failure } : snapshot;
  }
  const origin = snapshot.tabs.find((tab) => tab.id === opening.origin);
  if (!origin?.feedback || origin.feedback.id !== opening.feedbackId) return snapshot;
  return replaceTab(snapshot, { ...origin, feedback: { id: origin.feedback.id, failure } });
}

export function useFileViewerTabs(intent: FileViewerIntent) {
  const [state, setState] = useState<ReaderState>(() => ({
    tabs: intent.mode === "picker" ? [] : [{ ...openingTab(1, 1, { request: intent, action: 1 }), initial: true }],
    active: intent.mode === "picker" ? undefined : 1,
    action: 1,
    picker: intent.mode === "picker" ? { action: 1 } : undefined,
    reuseChecks: [],
  }));
  // Confirmation and render callbacks can settle before React publishes a render.
  const current = useRef(state);
  const [actions] = useState(() => {
    let lastTabId = 1;
    let lastAttempt = 1;
    const commit = (next: ReaderState) => {
      current.current = next;
      setState(next);
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
    const failTab = (snapshot: ReaderState, tab: ReaderTab, error: OpenError) => {
      const confirmed = tab.opening.confirmed;
      const firstFileCanRecover = tab.initial &&
        !(confirmed && snapshot.tabs.some((other) => other !== tab && matchesReadyFile(other, confirmed)));
      if (tab.reload || (tab.opening.origin === undefined && (snapshot.active === tab.id || firstFileCanRecover))) {
        return replaceTab(snapshot, { ...tab, phase: "recovery", error, slow: undefined, retained: undefined, positionNotice: undefined });
      }
      return reportOpeningFailure(removeTab(snapshot, tab), tab.opening, error);
    };
    const reuse = (snapshot: ReaderState, target: ReaderTab, candidate: ReaderAttempt) => {
      if (candidate.opening.action !== snapshot.action) return snapshot;
      const position = candidate.position;
      if (position === undefined) return activateTab(snapshot, target.id);
      const cleared = clearReadingFeedback(target, true);
      const positioned = position
        ? updateDisplayedLocation(cleared, { positionCheck: { position, action: snapshot.action } })
        : positionTab(cleared, null, snapshot.action);
      return activateTab(replaceTab(snapshot, positioned), target.id);
    };
    const confirm = (snapshot: ReaderState, candidate: ReaderTab, file: FileViewerMetadata, resolved?: Opening["resolved"]) => {
      const reserved = snapshot.tabs.includes(candidate);
      const tab = { ...candidate,
        opening: { ...candidate.opening, confirmed: file, resolved: resolved ?? candidate.opening.resolved },
        position: candidate.position === undefined ? resolved?.target.position ?? undefined : candidate.position };
      const target = !tab.reload && snapshot.tabs.find((other) => other.id !== tab.id && matchesReadyFile(other, file));
      if (target) return reuse(reserved ? removeTab(snapshot, candidate) : snapshot, target, tab);
      if (reserved) return replaceTab(snapshot, tab);
      if (snapshot.tabs.length >= TAB_LIMIT) {
        return tab.opening.action === snapshot.action ? { ...snapshot, notice: NO_SLOT } : snapshot;
      }
      return appendTab(snapshot, tab);
    };
    const start = (request: FileViewerRequest, origin?: ReaderTab, {
      reload = false, previous, confirmationRequest, previousTarget = previous?.confirmed ?? previous?.previousTarget,
    }: { reload?: boolean; previous?: Opening; confirmationRequest?: FileViewerRequest; previousTarget?: ConfirmedTarget } = {}) => {
      const snapshot = current.current;
      const replacing = origin && (reload || origin.phase === "recovery");
      const action = snapshot.action + 1;
      const attempt = ++lastAttempt;
      const feedbackId = !replacing && origin && displayedReaderTab(origin).phase === "ready" ? attempt : undefined;
      const clearedOrigin = origin && clearReadingFeedback(origin);
      const opening: Opening = {
        request: confirmationRequest?.reloadSource ? { ...request, reloadSource: confirmationRequest.reloadSource } : request,
        action, origin: feedbackId === undefined ? undefined : origin?.id, feedbackId,
        confirmed: confirmationRequest ? undefined : previous?.confirmed, previousTarget, resolved: previous?.resolved,
      };
      const confirming = confirmationRequest ?? request;
      const couldReuse = !replacing && !opening.confirmed && snapshot.tabs.some((tab) => knownReadyTarget(tab, confirming));
      const acting = { ...snapshot, action, picker: undefined, readerFailure: undefined };
      const cleared = clearedOrigin ? replaceTab(acting, clearedOrigin) : acting;
      if (!replacing && !couldReuse && snapshot.tabs.length >= TAB_LIMIT) {
        commit({ ...cleared, notice: NO_SLOT });
        return;
      }
      const nextState = { ...cleared, notice: undefined };
      const next = clearedOrigin && feedbackId !== undefined
        ? replaceTab(nextState, { ...clearedOrigin, feedback: { id: feedbackId } })
        : nextState;
      if (couldReuse) {
        // Current access still needs confirmation, even when all body slots are occupied.
        commit({ ...next, reuseChecks: [...next.reuseChecks, { ...opening, attempt, confirmationRequest: confirming }] });
        return;
      }
      const created = {
        ...openingTab(replacing ? origin.id : ++lastTabId, attempt, opening, confirmationRequest),
        initial: replacing ? origin.initial : undefined,
      };
      const tab = replacing && reload && clearedOrigin && (origin.reload || displayedReaderTab(origin).phase === "ready")
        ? reloadTab(clearedOrigin, created) : created;
      const installed = replacing ? replaceTab(next, tab) : appendTab(next, tab);
      commit(request.mode === "edit" ? activateTab(installed, tab.id) : installed);
    };
    const restart = (tab: ReaderTab, action = tab.opening.action): ReaderTab => ({
      ...tab, attempt: ++lastAttempt, opening: { ...tab.opening, action }, request: { ...tab.request },
      phase: "opening", error: undefined, slow: undefined,
    });
    const reconfirm = (opening: Opening, request = opening.request): FileViewerRequest => {
      const { confirmed, previousTarget, resolved } = opening;
      const target = confirmed ?? previousTarget;
      const reloadSource = target?.source ?? request.reloadSource;
      return usesFileResolver(request) && (request.mode === "description" || resolved?.description) && target
        ? { ...failedFileRead(opening), path: target.absolute_path, mode: undefined, reloadSource }
        : { ...request, reloadSource };
    };
    const retryFile = (failure: OpeningFailure) => ({
      previous: failure,
      confirmationRequest: failure.recovery === "reconfirm" ? reconfirm(failure) : undefined,
    });
    return {
      owns: (request: FileViewerRequest) => isCurrentFileViewer(intent) &&
        current.current.tabs.some((tab) => tab.request === request || tab.retained?.request === request),
      canInteract,
      canEditPath,
      open(origin: FileViewerRequest, path: string, options: FileViewerOptions = {}) {
        const tab = activeTab(origin);
        if (tab) start({ ...options, path, sessionId: intent.sessionId }, tab);
      },
      edit(origin: FileViewerRequest, path: string, options: FileViewerOptions, samePath: boolean) {
        const tab = activeTab(origin);
        if (!tab || tab.retained) return;
        const original = tab.opening.request;
        const editedRequest = { ...options, path, sessionId: intent.sessionId };
        if (samePath && usesFileResolver(original)) {
          // Recheck the draft's target now and keep the command for future reloads.
          start({ ...original, position: options.position }, tab, {
            reload: true, previous: tab.opening, confirmationRequest: editedRequest,
          });
        } else {
          start(editedRequest, tab, { reload: samePath });
        }
      },
      reload(request: FileViewerRequest) {
        const tab = activeTab(request);
        if (!tab) return;
        const displayed = displayedReaderTab(tab);
        if (displayed.phase === "recovery") {
          const snapshot = current.current;
          const action = snapshot.action + 1;
          const next = restart(tab, action);
          if (tab.error?.recovery === "reconfirm") {
            // Retry rechecks the current attempt. Reload below starts from the original command.
            next.request = reconfirm(tab.opening, tab.request);
            next.opening = { ...next.opening, confirmed: undefined,
              previousTarget: tab.opening.confirmed ?? tab.opening.previousTarget,
              request: { ...next.opening.request, reloadSource: next.request.reloadSource } };
          }
          commit(replaceTab({ ...snapshot, action }, next));
          return;
        }
        if (displayed.phase !== "ready") return;
        const { request: original, confirmed, resolved } = displayed.opening;
        // Reload a description's selected path with current authorization. Only an explicit search runs inference again.
        const description = resolved?.description ?? (original.mode === "description" ? original.path : undefined);
        const originalRequest = { ...original, position: displayed.position };
        const confirmation = description && confirmed ? {
          path: confirmed.absolute_path, source: confirmed.source, sessionId: original.sessionId, description,
        } : originalRequest;
        start(originalRequest, tab, {
          reload: true, previous: description ? displayed.opening : undefined,
          previousTarget: confirmed,
          confirmationRequest: { ...confirmation, reloadSource: confirmed?.source },
        });
      },
      pick(picker: number, path: string, options: FileViewerOptions) {
        const snapshot = current.current;
        if (isCurrentFileViewer(intent) && snapshot.picker?.action === picker) {
          const origin = snapshot.tabs.find((tab) => tab.id === snapshot.picker?.origin);
          start({ ...options, path, sessionId: intent.sessionId }, origin);
        }
      },
      retryReaderFailure(failure: OpeningFailure) {
        if (!isCurrentFileViewer(intent) || current.current.picker || current.current.readerFailure !== failure) return;
        start(failure.request, undefined, retryFile(failure));
      },
      editReaderFailure(failure: OpeningFailure) {
        if (!isCurrentFileViewer(intent) || current.current.picker || current.current.readerFailure !== failure) return;
        start({ ...failedFileRead(failure), mode: "edit" });
      },
      retryReuse(attempt: number) {
        const snapshot = current.current;
        if (!isCurrentFileViewer(intent) || snapshot.picker) return;
        const check = snapshot.reuseChecks.find((item) => item.attempt === attempt);
        if (!check) return;
        const replacement = { ...check, attempt: ++lastAttempt };
        commit({ ...snapshot, reuseChecks: snapshot.reuseChecks.map((item) => item === check ? replacement : item) });
      },
      settleReuse(attempt: number, result?: { meta: FileViewerMetadata } | { error: OpenError }) {
        const snapshot = current.current;
        if (!isCurrentFileViewer(intent)) return;
        const opening = snapshot.reuseChecks.find((item) => item.attempt === attempt);
        if (!opening) return;
        const next = { ...snapshot, reuseChecks: snapshot.reuseChecks.filter((other) => other !== opening) };
        if (!result) {
          commit(next);
          return;
        }
        if ("error" in result) {
          commit(reportOpeningFailure(next, opening, result.error));
          return;
        }
        commit(confirm(next, openingTab(++lastTabId, ++lastAttempt, opening, opening.confirmationRequest), result.meta));
      },
      confirm(request: FileViewerRequest, meta: FileViewerMetadata, resolved?: Opening["resolved"]) {
        const tab = owned(request);
        if (tab?.phase === "opening") commit(confirm(current.current, tab, meta, resolved));
      },
      ready(request: FileViewerRequest) {
        const tab = owned(request);
        if (tab?.phase !== "opening" || !tab.opening.confirmed) return;
        const snapshot = current.current;
        const file = tab.opening.confirmed;
        const target = snapshot.tabs.find((other) => other.id !== tab.id && matchesReadyFile(other, file));
        if (target) {
          if (tab.reload) {
            commit(failTab(snapshot, tab, { message: "This file is already open in another tab.", recovery: "edit" }));
          } else {
            commit(reuse(removeTab(snapshot, tab), target, tab));
          }
          return;
        }
        const activate = !tab.reload && tab.opening.action === snapshot.action;
        const ready: ReaderTab = { ...tab, phase: "ready", slow: undefined, reload: undefined, retained: undefined,
          position: tab.reload || activate ? tab.position : undefined };
        const next = replaceTab(snapshot, ready);
        commit(activate ? activateTab(next, tab.id) : next);
      },
      fail(request: FileViewerRequest, error: OpenError) {
        const tab = owned(request);
        if (tab?.phase === "opening") commit(failTab(current.current, tab, error));
      },
      retryFailure(origin: FileViewerRequest, failure: OpeningFailure) {
        const tab = activeTab(origin);
        if (tab?.feedback?.failure === failure) start(failure.request, tab, retryFile(failure));
      },
      canEditFailure(origin: FileViewerRequest, failure: OpeningFailure) {
        return activeTab(origin)?.feedback?.failure === failure;
      },
      editFailure(origin: FileViewerRequest, failure: OpeningFailure, path: string, options: FileViewerOptions) {
        const tab = activeTab(origin);
        if (tab && tab.id === failure.origin) start({ ...options, path, sessionId: intent.sessionId }, tab);
      },
      dismissFailure(origin: FileViewerRequest, failure: OpeningFailure) {
        const tab = activeTab(origin);
        if (tab?.feedback?.failure === failure) commit(replaceTab(current.current, { ...tab, feedback: undefined }));
      },
      dismissPosition(request: FileViewerRequest, id: string) {
        const tab = activeTab(request);
        if (tab?.positionNotice?.id === id) {
          commit(replaceTab(current.current, { ...tab, positionNotice: undefined }));
        }
      },
      reportPositionFailure(request: FileViewerRequest, action: number, message: string) {
        const tab = activeTab(request);
        if (!tab || displayedReaderTab(tab).positionReport !== action) return;
        const checked = updateDisplayedLocation(tab, { positionReport: undefined });
        commit(replaceTab(current.current, showPositionNotice(checked, action, message)));
      },
      settlePosition(request: FileViewerRequest, check: PositionCheck, note?: string) {
        const tab = activeTab(request);
        if (!tab || displayedReaderTab(tab).positionCheck !== check) return;
        const next = note ? showPositionNotice(updateDisplayedLocation(tab, { positionCheck: undefined, invalidLocation: note }), check.action, note)
          : positionTab(tab, check.position, check.action);
        commit(replaceTab(current.current, next));
      },
      markSlow(request: FileViewerRequest) {
        const tab = owned(request);
        if (tab?.phase === "opening" && !tab.slow) commit(replaceTab(current.current, { ...tab, slow: true }));
      },
      retryPending(request: FileViewerRequest) {
        const tab = owned(request);
        const snapshot = current.current;
        if (!tab || !canRetryPendingFile(tab) || snapshot.picker ||
          (!tab.slow && !canInteract(request))) return;
        commit(replaceTab(snapshot, restart(tab)));
      },
      select(request: FileViewerRequest) {
        const tab = owned(request);
        const snapshot = current.current;
        if (!tab || snapshot.picker || (!selectableReaderTab(tab) && snapshot.active !== tab.id)) return;
        commit({ ...activateTab(snapshot, tab.id), action: snapshot.action + 1 });
      },
      locate(request: FileViewerRequest, position?: FilePosition | null, invalidLocation?: string) {
        const tab = activeTab(request);
        if (!tab) return false;
        const snapshot = current.current;
        const action = snapshot.action + 1;
        const next = clearReadingFeedback(tab, !!invalidLocation);
        const located = invalidLocation ? showPositionNotice(updateDisplayedLocation(next, { invalidLocation }), action, invalidLocation)
          : position === undefined ? next : positionTab(next, position, action);
        commit(replaceTab({ ...snapshot, action }, located));
        return true;
      },
      close(request: FileViewerRequest) {
        const tab = owned(request);
        if (tab) commit({ ...removeTab(current.current, tab), notice: undefined });
      },
      showPicker(origin?: FileViewerRequest) {
        if (!isCurrentFileViewer(intent) || (origin && !canInteract(origin))) return;
        const snapshot = current.current;
        if (snapshot.picker) return;
        const tab = snapshot.tabs.find((tab) => tab.id === snapshot.active);
        const displayed = tab && displayedReaderTab(tab);
        const meta = displayed?.phase === "ready" ? displayed.opening.confirmed : undefined;
        const location = meta?.canonical_directory ? { source: meta.source, directory: meta.canonical_directory } : undefined;
        const action = snapshot.action + 1;
        const next = tab ? replaceTab(snapshot, clearReadingFeedback(tab)) : snapshot;
        commit({ ...next, picker: { action, origin: tab?.id, location }, action, notice: undefined });
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
