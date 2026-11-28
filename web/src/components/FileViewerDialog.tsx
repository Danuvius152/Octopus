import { accountFetch as fetch } from "../lib/accountFetch";
/**
 * Modal lightbox that renders a file from an authorized source.
 *
 * Mounted once at the App level; opens when sessionStore.viewer is
 * non-null. The viewer retains the requested target while it loads.
 *
 * Renderer dispatch (kind from checked file metadata):
 *   markdown → react-markdown + remark-gfm + rehype-highlight
 *   code     → <pre><code class="language-x"> through highlight.js
 *   text     → <pre> plain
 *   image    → <img> with click-to-zoom
 *   pdf      → PDF.js reader in an <iframe>
 *
 * The session file API supplies metadata and bytes. Link actions and
 * embedded Markdown images authenticate with a query token.
 *
 * Visual reference: VM0's AttachmentLightbox
 * (vm0/turbo/apps/platform/src/views/zero-page/zero-attachment-chips.tsx)
 * — centered modal, ~1100px wide pane, dark backdrop, header w/
 * filename + actions in the top-right.
 */

import { memo, useCallback, useEffect, useEffectEvent, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ClipboardEvent, Dispatch, DragEvent, KeyboardEvent, ReactNode, HTMLAttributes, SetStateAction } from "react";
import { createPortal } from "react-dom";
import {
  IconCheck,
  IconChevronDown,
  IconCopy,
  IconDownload,
  IconExternalLink,
  IconFile,
  IconPlus,
  IconRefresh,
  IconTerminal2,
  IconX,
  IconZoomIn,
  IconZoomOut,
} from "@tabler/icons-react";
import Markdown, { type Components } from "react-markdown";
import { normalizeUri } from "micromark-util-sanitize-uri";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import rehypeSlug from "rehype-slug";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";

import "highlight.js/styles/github.css";
import "katex/dist/katex.min.css";
import "./FileViewerDialog.css";

import type { FilePosition, FileResourceDirectory, FileViewerMetadata, ShowMeEditTarget, ShowMeFileErrorResponse, ShowMeResolveResponse } from "../api";
import { useSessionStore, type FileViewerIntent, type FileViewerOptions } from "../stores/sessionStore";
import { cn } from "../lib/utils";
import { decodeFileReference, filePositionSuffix, parseFilePosition, splitFileLink } from "../lib/filePosition";
import { documentLinkUrl, readFileReference, type FileReference } from "../lib/fileReference";
import { FileReferenceButton } from "./MarkdownFileLink";
import { FileSourceView, linePositionNote } from "./FileSourceView";
import { FilePicker } from "./FilePicker";
import { PdfReaderFrame } from "./PdfReaderFrame";
import { applyReaderSize, type ReportFilePosition } from "./fileReaderScroll";
import {
  canRetryPendingFile, displayedReaderTab, failedFileRead, readerTabName, selectableReaderTab, useFileViewerTabs, usesFileResolver,
  type FileViewerRequest, type FileViewerTabActions, type OpenError, type Opening, type OpeningFailure, type ReaderAttempt, type ReaderTab,
} from "./fileViewerTabs";

interface FileUrlOptions {
  metadata?: boolean;
  source?: string;
  sourceDirectory?: FileResourceDirectory;
  canonicalPathId?: string;
  reloadSource?: string;
}

function fileUrl(
  sessionId: string,
  path: string,
  { metadata = false, source, sourceDirectory, canonicalPathId, reloadSource }: FileUrlOptions = {},
): string {
  const endpoint = metadata ? "files/meta" : "files";
  const query = new URLSearchParams({ path });
  // Use a fresh cache key for byte URLs issued before responses used no-store.
  if (!metadata) query.set("viewer", "1");
  if (source) query.set("source", source);
  if (reloadSource) query.set("reload_source", reloadSource);
  if (canonicalPathId) query.set("canonical_path_id", canonicalPathId);
  if (sourceDirectory) {
    query.set("source_directory", sourceDirectory.path);
    query.set("source_directory_id", sourceDirectory.path_id);
  }
  return `${window.location.origin}/api/sessions/${encodeURIComponent(sessionId)}/${endpoint}?${query}`;
}

function confirmedFileUrl({ sessionId, sourceDirectory }: FileViewerRequest, file: FileViewerMetadata) {
  return fileUrl(sessionId, file.path, { source: file.source, sourceDirectory, canonicalPathId: file.canonical_path_id });
}

class FileReadFailure extends Error {
  readonly recovery: NonNullable<OpenError["recovery"]>;
  readonly denied: boolean;
  readonly originalPdf?: FileViewerMetadata;

  constructor(message: string, recovery: NonNullable<OpenError["recovery"]>, { denied = false, originalPdf }: {
    denied?: boolean; originalPdf?: FileViewerMetadata;
  } = {}) {
    super(message);
    this.recovery = recovery;
    this.denied = denied;
    this.originalPdf = originalPdf;
  }
}

const UNSUPPORTED_LINK: OpenError = {
  message: "This file link uses an unsupported path format. Enter a path on the server.", recovery: "edit",
};

function fileStatusError(status: number, failure?: ShowMeFileErrorResponse): FileReadFailure {
  const detail = failure?.detail;
  switch (status) {
    case 0:
      return new FileReadFailure("Couldn't load the file. Check your connection and retry.", "retry");
    case 401:
    case 403:
      return new FileReadFailure("You don't have access to this file.", "edit", { denied: true });
    case 404:
      return new FileReadFailure(detail || "File not found.", "edit");
    case 409:
      if (failure?.code === "source_directory_changed") {
        return new FileReadFailure("The document directory changed. Reopen the source document.", "reopen-source");
      }
      return !detail || failure?.code === "target_changed"
        ? new FileReadFailure("The target file changed. Retry to check and read it again.", "reconfirm")
        : new FileReadFailure(detail, "retry");
    case 413:
      return new FileReadFailure("This file exceeds the 2 MiB limit for preview and download.", "edit");
    case 415:
      return new FileReadFailure("This file format can't be previewed.", "edit");
    default:
      return new FileReadFailure(detail || `Couldn't read the file (HTTP ${status}).`, status >= 500 || !detail ? "retry" : "edit");
  }
}

async function fileReadError(response: Response): Promise<FileReadFailure> {
  if (response.status === 401 || response.status === 403) return fileStatusError(response.status);
  return fileStatusError(response.status, await readFileErrorBody(response));
}

async function readFileMetadata(request: FileViewerRequest, init: RequestInit): Promise<FileViewerMetadata> {
  const { sessionId, path, source, sourceDirectory, reloadSource } = request;
  const response = await fetch(fileUrl(sessionId, path, { metadata: true, source, sourceDirectory, reloadSource }), init);
  if (!response.ok) throw await fileReadError(response);
  return await response.json() as FileViewerMetadata;
}

async function readFileErrorBody(response: Response): Promise<ShowMeFileErrorResponse | undefined> {
  const body = await response.json().catch(() => undefined);
  return typeof body?.detail === "string" ? body : undefined;
}

function readFailure(error: unknown): FileReadFailure {
  return error instanceof FileReadFailure ? error : fileStatusError(0);
}

function editablePath(viewer: FileViewerRequest, meta: FileViewerMetadata | null, target: ShowMeEditTarget | null): string {
  if (meta) return meta.absolute_path;
  if (target) return target.path;
  if (viewer.mode === "command" && viewer.path.startsWith('"')) {
    try { return JSON.parse(viewer.path) as string; }
    catch { return viewer.path; }
  }
  return viewer.path;
}

function filePathSegments(path: string) {
  return path.split("/").filter((segment) => segment && segment !== ".");
}

function sourceContainingPath(source: string | undefined, target: string[]) {
  return source && filePathSegments(source).every((segment, index) => segment === target[index]) ? source : undefined;
}

function positionNoteFor(position: FilePosition | undefined, kind: FileViewerMetadata["kind"]): string | undefined {
  if (!position) return;
  if (position.kind === "invalid") return "The requested file position is invalid.";
  if (kind === "image" || kind === "pdf") return "Text positions aren't available for this file type.";
  if (position.kind === "heading" && kind !== "markdown") return "Heading positions are only available for Markdown.";
}

// A textarea normalizes CR and CRLF to LF. Escape CR in its editable value so
// selection, typing and native undo all operate on an unambiguous string.
function escapePathForEditing(path: string): string {
  return path.replace(/[\r␛]/g, (character) => character === "\r" ? "␛r" : "␛␛");
}

function unescapeEditedPath(draft: string): string {
  return draft.replace(/␛([r␛])/g, (_, escape) => escape === "r" ? "\r" : "␛");
}

function fileEditDraft(path: string, position: FilePosition | null | undefined,
  binding: { failure?: OpeningFailure; openingRequest?: FileViewerRequest } = {}) {
  return { initialPath: path, path: escapePathForEditing(path), position: filePositionSuffix(position ?? undefined),
    dropNotice: false, ...binding };
}

type PathDraft = ReturnType<typeof fileEditDraft>;

function pathTokenSelection(draft: string, start: number, end: number) {
  const collapsed = start === end;
  // Decode only whole tokens. A caret inside a token moves to its end.
  for (const token of draft.matchAll(/␛[r␛]/g)) {
    const middle = token.index + 1;
    if (start === middle) start += collapsed ? 1 : -1;
    if (end === middle) end++;
  }
  return { start, end };
}

function focusTextarea(field: HTMLTextAreaElement | null) {
  field?.focus();
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

// Map file extension → highlight.js language hint so the fenced
// <code> block gets the right grammar. Falls back to autodetection
// (rehype-highlight) when not in the table.
function languageHint(filename: string): string | null {
  const ext = filename.toLowerCase().split(".").pop() ?? "";
  const table: Record<string, string> = {
    py: "python",
    pyi: "python",
    js: "javascript",
    jsx: "javascript",
    ts: "typescript",
    tsx: "typescript",
    mjs: "javascript",
    cjs: "javascript",
    rs: "rust",
    go: "go",
    java: "java",
    kt: "kotlin",
    c: "c",
    h: "c",
    cpp: "cpp",
    hpp: "cpp",
    cs: "csharp",
    rb: "ruby",
    php: "php",
    swift: "swift",
    scala: "scala",
    sh: "bash",
    bash: "bash",
    zsh: "bash",
    sql: "sql",
    html: "html",
    css: "css",
    scss: "scss",
    less: "less",
    vue: "xml",
    lua: "lua",
    r: "r",
    dart: "dart",
    toml: "toml",
    yaml: "yaml",
    yml: "yaml",
    json: "json",
    xml: "xml",
    ini: "ini",
    tf: "hcl",
  };
  return table[ext] ?? null;
}

function restoreFocus(element: HTMLElement | null | undefined) {
  if (!element?.isConnected || element === document.body || element.matches(":disabled") ||
      element.closest('[inert], [aria-hidden="true"]')) return false;
  element.focus({ preventScroll: true });
  return document.activeElement === element;
}

const READER_SIZES = { compact: "Compact", standard: "Standard", large: "Large", larger: "Largest" };
type ReaderSize = keyof typeof READER_SIZES;

function isReaderSize(value: string | null): value is ReaderSize {
  return value !== null && Object.hasOwn(READER_SIZES, value);
}

function readReaderSize(key: string | undefined): ReaderSize {
  if (key) {
    try {
      const value = localStorage.getItem(key);
      if (isReaderSize(value)) return value;
    } catch {
      // The current viewer can still adjust its size when storage is unavailable.
    }
  }
  return "standard";
}

export function FileViewerDialog() {
  const viewer = useSessionStore((s) => s.viewer);
  const closeViewer = useSessionStore((s) => s.closeViewer);
  const token = useSessionStore((s) => s.token);
  const focusReturn = useRef<{
    trigger: HTMLElement | null;
    dialog: HTMLElement | null;
    content: EventTarget | null;
    requested: boolean;
  } | null>(null);
  useEffect(() => useSessionStore.subscribe((current, previous) => {
    if (current.accountGeneration !== previous.accountGeneration ||
      current.activeSessionId !== previous.activeSessionId || current.mainView !== previous.mainView) {
      focusReturn.current = null;
    }
  }), []);
  const close = () => {
    if (focusReturn.current) focusReturn.current.requested = true;
    closeViewer();
  };

  const open = viewer !== null;
  const onOpenChange = (next: boolean) => {
    if (!next) close();
  };

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay
          className="fixed inset-0 z-[100] bg-black/70 backdrop-blur-sm data-[state=open]:animate-in data-[state=open]:fade-in-0"
        />
        <DialogPrimitive.Content
          aria-describedby={undefined}
          onOpenAutoFocus={(event) => {
            if (!viewer) return;
            const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
            focusReturn.current = {
              trigger, dialog: trigger?.closest('[role="dialog"]') ?? null, content: event.target, requested: false,
            };
            if (viewer.mode === "picker" && event.target instanceof HTMLElement) {
              event.preventDefault();
              event.target.querySelector<HTMLElement>("[data-file-picker-heading]")?.focus();
            } else if (viewer.mode === "edit" && event.target instanceof HTMLElement) {
              event.preventDefault();
              event.target.querySelector("textarea")?.focus();
            }
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            const target = focusReturn.current;
            if (useSessionStore.getState().viewer || !target || target.content !== event.target) return;
            focusReturn.current = null;
            if (!target.requested) return;
            if (restoreFocus(target.trigger)) return;
            const dialog = target.dialog;
            if (dialog?.dataset.state === "open" &&
                (restoreFocus(dialog.querySelector<HTMLElement>("[data-dialog-close]")) || restoreFocus(dialog))) return;
            restoreFocus(document.querySelector<HTMLElement>("[data-chat-session]:not([inert]) .chat-input-bar textarea:not(:disabled), [data-chat-session]:not([inert]) [data-open-session-file]"));
          }}
          className={cn(
            "fixed left-[50%] top-[50%] z-[100] flex flex-col",
            "translate-x-[-50%] translate-y-[-50%]",
            "file-viewer w-[min(1100px,95vw)] h-[min(85dvh,900px)]",
            "rounded-xl border border-border bg-card shadow-2xl",
            "focus:outline-none"
          )}
        >
          {viewer && (
            <FileViewerTabs
              key={viewer.id}
              viewer={viewer}
              token={token}
              onClose={close}
            />
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

interface InnerProps {
  viewer: FileViewerIntent;
  token: string;
  onClose: () => void;
}

const SLOW_FILE_READ_MS = 5_000;

function FileViewerTabs({ viewer, token, onClose }: InnerProps) {
  const { state, actions } = useFileViewerTabs(viewer);
  const { tabs, active, picker } = state;
  const frame = useRef<HTMLDivElement>(null);
  const choose = useRef<HTMLButtonElement>(null);
  const lastFocused = useRef<HTMLElement | null>(null);
  const focusTabAfterClose = useRef(false);
  const pickerAction = picker?.action;
  const wasPicker = useRef(pickerAction);
  const identityId = useSessionStore((s) => s.identity?.user_id);
  const sizeKey = identityId === undefined ? undefined
    : identityId === null ? "octopus_file_reader_size:legacy" : `octopus_file_reader_size:account:${identityId}`;
  const savedSize = useMemo(() => readReaderSize(sizeKey), [sizeKey]);
  const [preference, setPreference] = useState<{ key: string; size: ReaderSize }>();
  const readerSize = preference && preference.key === sizeKey ? preference.size : savedSize;
  const changeReaderSize = (value: string) => {
    if (!sizeKey || !isReaderSize(value)) return;
    setPreference({ key: sizeKey, size: value });
    try { localStorage.setItem(sizeKey, value); } catch { /* Keep the in-view preference. */ }
  };
  const panelId = (id: number) => `file-viewer-${viewer.id}-panel-${id}`;
  const selectableTabs = tabs.filter((tab) => selectableReaderTab(tab) || tab.id === active);
  const tabButton = (id: number | undefined) => frame.current?.querySelector<HTMLElement>(`[data-file-tab="${id}"]`);
  const pendingTabs = tabs.filter((tab) => !selectableReaderTab(tab) && tab.id !== active);
  const feedback = [
    ...state.reuseChecks,
    ...tabs.flatMap((tab) => tab.id === active && tab.feedback?.failure ? [tab.feedback.failure] : []),
    ...(state.readerFailure ? [state.readerFailure] : []),
  ];
  const pendingNames = pendingTabs.map((tab) => accessibleTargetName(readerTabName(tab)));
  const labels = openingLabels([
    ...feedback.map((opening) => ({ opening, name: opening.request.path })),
    ...pendingTabs.map((tab, index) => ({
      opening: tab.opening,
      name: pendingNames.some((name, other) => other !== index && name === pendingNames[index])
        ? tab.request.path : readerTabName(tab),
    })),
  ]);

  useLayoutEffect(() => {
    const previousPicker = wasPicker.current;
    wasPicker.current = pickerAction;
    if (pickerAction && pickerAction !== previousPicker) {
      frame.current?.querySelector<HTMLElement>("[data-file-picker-heading]")?.focus();
    } else if (previousPicker && !pickerAction) {
      choose.current?.focus();
    } else if (!pickerAction && (focusTabAfterClose.current || lastFocused.current?.closest("[data-file-panel][inert]") ||
      (lastFocused.current && !lastFocused.current.isConnected && !frame.current?.contains(document.activeElement)))) {
      const tab = frame.current?.querySelector<HTMLElement>(`[data-file-tab="${active}"]`);
      (tab ?? choose.current)?.focus();
    }
    focusTabAfterClose.current = false;
  }, [active, pickerAction, tabs, state.reuseChecks]);

  const closeTab = (tab: ReaderTab) => {
    focusTabAfterClose.current = active === tab.id || lastFocused.current?.closest(`[data-file-slot="${tab.id}"]`) != null;
    actions.close(tab.request);
  };
  const onTabKey = (event: KeyboardEvent<HTMLButtonElement>, tab: ReaderTab) => {
    if (event.key === "Delete") {
      event.preventDefault();
      closeTab(tab);
      return;
    }
    const index = selectableTabs.indexOf(tab);
    let next: ReaderTab | undefined;
    switch (event.key) {
      case "ArrowLeft": next = selectableTabs.at(index - 1); break;
      case "ArrowRight": next = selectableTabs[(index + 1) % selectableTabs.length]; break;
      case "Home": next = selectableTabs[0]; break;
      case "End": next = selectableTabs.at(-1); break;
      default: return;
    }
    event.preventDefault();
    if (!next) return;
    actions.select(next.request);
    tabButton(next.id)?.focus();
  };

  return <div ref={frame} className="flex min-h-0 min-w-0 flex-1 flex-col" onFocusCapture={(event) => {
    if (event.target instanceof HTMLElement) lastFocused.current = event.target;
  }}>
    {!picker && <div className="flex shrink-0 items-center gap-2 border-b border-border px-2 py-1.5">
      {tabs.length > 0 && <div role="tablist" aria-label="Open files" className="flex min-w-0 items-center gap-1 overflow-x-auto p-1">
        {tabs.map((tab) => {
          const displayed = displayedReaderTab(tab);
          const pending = !selectableReaderTab(tab) && tab.id !== active;
          const title = readerTabName(tab);
          const name = pending ? labels.get(tab.opening) ?? title : title;
          const retryLabel = tab.reload ? `Retry reloading ${name}` : `Retry opening ${name}`;
          return <div key={tab.id} role="presentation" data-file-slot={tab.id}
            className={cn("flex shrink-0 items-center rounded-md text-sm", tab.id === active && "bg-accent")}>
            {pending ? <span role="status" className="max-w-44 truncate px-2 text-muted-foreground" title={tab.request.path}>
              {tab.slow ? `${name} is taking longer to load` : `${name} loading…`}
            </span> : <button type="button" role="tab" id={`${panelId(tab.id)}-tab`} data-file-tab={tab.id}
              aria-controls={panelId(tab.id)} aria-selected={tab.id === active} tabIndex={tab.id === active || (active === undefined && tab.id === selectableTabs[0]?.id) ? 0 : -1}
              title={displayed.opening.confirmed?.absolute_path ?? displayed.request.path}
              className="max-w-44 truncate rounded-md px-2 py-1"
              onClick={() => actions.select(tab.request)} onKeyDown={(event) => onTabKey(event, tab)}>{name}</button>}
            {(pending || tab.reload) && tab.slow && canRetryPendingFile(tab) && <button type="button" className={`${fileToolClass} !size-6`}
              title={retryLabel} aria-label={retryLabel} onClick={() => actions.retryPending(tab.request)}>
              <IconRefresh size={14} aria-hidden="true" />
            </button>}
            <button type="button" className={`${fileToolClass} !size-6`} tabIndex={pending || tab.id === active ? 0 : -1}
              title={pending ? `Cancel opening ${name}` : `Close ${name}`} aria-label={pending ? `Cancel opening ${name}` : `Close ${name}`}
              onClick={() => closeTab(tab)}><IconX size={14} aria-hidden="true" /></button>
          </div>;
        })}
      </div>}
      <button ref={choose} type="button" aria-label={active === undefined ? "Choose a file" : "Open file picker"} title="Choose a file" className={fileToolClass}
        onClick={() => actions.showPicker()}><IconPlus size={17} aria-hidden="true" /></button>
      {active === undefined && <button type="button" className={`${fileToolClass} ml-auto`} aria-label="Close" title="Close" onClick={onClose}>
        <IconX size={18} aria-hidden="true" />
      </button>}
    </div>}
    {state.reuseChecks.map((check) => <ConfirmFileReuse key={check.attempt} attempt={check.attempt}
      request={check.confirmationRequest}
      label={state.reuseChecks.length > 1 || labels.get(check) !== check.request.path ? labels.get(check) : undefined}
      token={token} settle={actions.settleReuse} retry={actions.retryReuse} hidden={!!picker} />)}
    {state.notice && <p role="status" className="px-4 py-2 text-sm text-muted-foreground">{state.notice}</p>}
    <div className="relative flex min-h-0 flex-1 flex-col">
      {picker && <FilePicker key={picker.action} viewer={viewer} token={token} initial={picker.location} onClose={onClose}
        onCancel={() => actions.hidePicker(picker.action)} onSelect={(path, options) => actions.pick(picker.action, path, options)} />}
      {!picker && active === undefined && <DialogPrimitive.Title className="px-5 py-4 text-sm font-semibold">Open file</DialogPrimitive.Title>}
      {!picker && state.readerFailure && <div className="border-b border-destructive/30 px-5 py-2 text-sm text-destructive">
        <OpeningFailureNotice failure={state.readerFailure} label={labels.get(state.readerFailure)} token={token}
          retry={actions.retryReaderFailure} edit={actions.editReaderFailure} />
      </div>}
      {tabs.map((tab) => <FileViewerPanel key={tab.id} tab={tab} panelId={panelId(tab.id)} visible={!picker && active === tab.id}
        openingLabels={labels} actions={actions} token={token} onClose={onClose}
        readerSize={readerSize} changeReaderSize={sizeKey ? changeReaderSize : undefined} />)}
    </div>
  </div>;
}

function FileViewerPanel({ tab, panelId, visible, openingLabels, actions, token, onClose, readerSize, changeReaderSize }: {
  tab: ReaderTab; panelId: string; visible: boolean; openingLabels: ReadonlyMap<Opening, string>;
  actions: FileViewerTabActions; token: string; onClose: () => void;
  readerSize: ReaderSize; changeReaderSize?: (value: string) => void;
}) {
  const [failureDraft, setFailureDraft] = useState<PathDraft | null>(null);
  const displayed = displayedReaderTab(tab);
  const failure = tab.feedback?.failure;
  const positionNotice = tab.positionNotice;
  return <section role="tabpanel" id={panelId} data-file-panel
    aria-labelledby={selectableReaderTab(tab) || visible ? `${panelId}-tab` : undefined}
    aria-hidden={!visible || undefined} inert={!visible}
    className={cn("flex min-h-0 min-w-0 flex-1 flex-col", !visible && "invisible absolute inset-0 pointer-events-none")}>
    {tab.retained && <p role="status" className="shrink-0 px-5 py-1 text-sm text-muted-foreground">
      {tab.slow && !tab.opening.confirmed ? "Reloading is taking longer…" : "Reloading file…"}
    </p>}
    {visible && failure && (
      <ReadingNotice key={failure.feedbackId} label="Dismiss file opening notice" className="border-destructive/30 text-destructive"
        dismiss={() => actions.dismissFailure(displayed.request, failure)}>
        <OpeningFailureNotice failure={failure} label={openingLabels.get(failure)} token={token}
          retry={(target) => actions.retryFailure(displayed.request, target)}
          edit={(target) => {
            if (!actions.canEditFailure(displayed.request, target)) return;
            const read = failedFileRead(target);
            setFailureDraft(fileEditDraft(read.path, read.position, { failure: target }));
          }} />
      </ReadingNotice>
    )}
    {visible && positionNotice && (
      <ReadingNotice key={positionNotice.id} label="Dismiss position notice"
        dismiss={() => actions.dismissPosition(displayed.request, positionNotice.id)}>
        <p role="note" className="text-muted-foreground">{positionNotice.message}</p>
      </ReadingNotice>
    )}
    {(tab.retained ? [tab.retained, tab] : [tab]).map((attempt) => {
      const shown = attempt === displayed;
      return <div key={attempt.attempt} aria-hidden={!shown || undefined} inert={!shown}
        className={cn("flex min-h-0 min-w-0 flex-1 flex-col", !shown && "invisible absolute inset-0 pointer-events-none")}>
        <FileViewerInner tab={attempt} failureDraft={shown ? failureDraft : null} setFailureDraft={setFailureDraft}
          retained={attempt === tab.retained} reload={attempt === tab ? tab.reload : undefined}
          panelId={`${panelId}-attempt-${attempt.attempt}`} active={visible && shown}
          actions={actions} token={token} onClose={onClose} readerSize={readerSize} changeReaderSize={changeReaderSize} />
      </div>;
    })}
  </section>;
}

function OriginalPdfLinks({ request, meta, token }: { request: FileViewerRequest; meta: FileViewerMetadata; token: string }) {
  const url = confirmedFileUrl(request, meta);
  const download = `${url}&token=${encodeURIComponent(token)}`;
  return <>
    <a className="underline" href={`${download}&preview=true`} target="_blank" rel="noreferrer">Open original PDF</a>
    <a className="underline" href={download} download={meta.path.split("/").at(-1)}>Download original PDF</a>
  </>;
}

function openingLabel(opening: Opening, openings: readonly Opening[]) {
  const path = opening.request.path;
  const others = openings.filter((other) => other !== opening && other.request.path === path);
  if (!others.length) return undefined;
  const file = opening.confirmed;
  if (!file) return `${path} (target unconfirmed)`;
  return others.some((other) => other.confirmed?.absolute_path === file.absolute_path)
    ? `${file.absolute_path} (${file.source})` : file.absolute_path;
}

function accessibleTargetName(target: string) {
  return target.trim().replace(/\s+/g, " ");
}

function openingLabels(entries: { opening: Opening; name: string }[]) {
  const openings = entries.map(({ opening }) => opening);
  const candidates = entries.map(({ opening, name }) => {
    const target = openingLabel(opening, openings) ?? name;
    return { opening, target, name: accessibleTargetName(target) };
  }).sort((a, b) => a.opening.action - b.opening.action);
  const reserved = new Set(candidates.map(({ name }) => name));
  const labels = new Map<Opening, string>();
  for (const { opening, target, name } of candidates) {
    // Accessible names collapse whitespace. Number collisions without reusing a literal name.
    if (candidates.filter((candidate) => candidate.name === name).length > 1) {
      let attempt = 1;
      let numbered = `${target}, attempt ${attempt}`;
      while (reserved.has(accessibleTargetName(numbered))) {
        attempt++;
        numbered = `${target}, attempt ${attempt}`;
      }
      reserved.add(accessibleTargetName(numbered));
      labels.set(opening, numbered);
    } else {
      labels.set(opening, target);
    }
  }
  return labels;
}

function OpeningFailureNotice({ failure, label = failure.request.path, token, retry, edit }: {
  failure: OpeningFailure; label?: string; token: string; retry: (failure: OpeningFailure) => void; edit?: (failure: OpeningFailure) => void;
}) {
  return <div>
    <p role="alert" className="whitespace-pre-wrap break-words">Couldn't open {label}. {failure.message}</p>
    <div className="flex flex-wrap gap-x-4">
      {(failure.recovery === "retry" || failure.recovery === "reconfirm") && <button type="button" className="break-all text-left underline" onClick={() => retry(failure)}>
        Retry opening {label}
      </button>}
      {failure.recovery === "edit" && edit && <button type="button" className="break-all text-left underline" onClick={() => edit(failure)}>
        Edit path for {label}
      </button>}
    {failure.originalPdf && <OriginalPdfLinks request={failure.request} meta={failure.originalPdf} token={token} />}
    </div>
  </div>;
}

const READING_NOTICE_MS = 8000;

function ReadingNotice({ children, label, dismiss, className }: {
  children: ReactNode; label: string; dismiss: () => void; className?: string;
}) {
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const remaining = useRef(READING_NOTICE_MS);
  const expire = useEffectEvent(dismiss);
  const paused = hovered || focused;
  useEffect(() => {
    if (paused) return;
    const started = Date.now();
    const timer = window.setTimeout(() => expire(), remaining.current);
    return () => {
      window.clearTimeout(timer);
      remaining.current = Math.max(0, remaining.current - Math.max(0, Date.now() - started));
    };
  }, [paused]);
  return <div className={cn("flex max-h-28 shrink-0 items-start justify-between gap-3 overflow-y-auto border-b px-5 py-2 text-sm", className)}
    onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)}
    onFocus={() => setFocused(true)} onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false);
    }}>
    <div className="min-w-0">{children}</div>
    <button type="button" className={fileToolClass} aria-label={label} title={label} onClick={dismiss}>
      <IconX size={16} />
    </button>
  </div>;
}

function ConfirmFileReuse({ attempt, request, label, token, settle, retry, hidden }: {
  attempt: number;
  request: FileViewerRequest;
  label?: string;
  token: string;
  settle: FileViewerTabActions["settleReuse"];
  retry: FileViewerTabActions["retryReuse"];
  hidden: boolean;
}) {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    const abort = new AbortController();
    const timer = window.setTimeout(() => setSlow(true), SLOW_FILE_READ_MS);
    void (async () => {
      try {
        await Promise.resolve();
        if (abort.signal.aborted) return;
        const meta = await readFileMetadata(request, {
          headers: { Authorization: `Bearer ${token}` }, cache: "no-store", signal: abort.signal,
        });
        if (!abort.signal.aborted) settle(attempt, { meta });
      } catch (error) {
        if (!abort.signal.aborted) settle(attempt, { error: readFailure(error) });
      }
    })();
    return () => {
      window.clearTimeout(timer);
      abort.abort();
    };
  }, [attempt, request, settle, token]);
  if (hidden) return null;
  return <div className="flex shrink-0 items-center gap-2 px-4 py-1 text-sm">
    <span role="status" className="min-w-0 truncate" title={label ?? request.path}>
      {slow ? `Still confirming ${label ?? request.path}…` : `Confirming ${label ?? request.path}…`}
    </span>
    {slow && <button type="button" className="shrink-0 underline" aria-label={label ? `Retry confirmation for ${label}` : undefined}
      onClick={() => retry(attempt)}>Retry confirmation</button>}
    <button type="button" className="shrink-0 underline" aria-label={label ? `Cancel confirmation for ${label}` : undefined}
      onClick={() => settle(attempt)}>Cancel confirmation</button>
  </div>;
}

type FileAction = { label: string; icon: ReactNode } & (
  { run: () => void; disabled?: boolean; movesFocus?: boolean } |
  { href: string; download?: string; target?: "_blank" }
);
const fileToolClass = "flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50";

function FileActions({ actions }: { actions: FileAction[] }) {
  const afterMenuClose = useRef<(() => void) | null>(null);
  return (
    <>
      <div className="file-viewer-actions items-center gap-1">
        {actions.map((action) => "href" in action ? (
          <a key={action.label} href={action.href} download={action.download} target={action.target}
            rel={action.target ? "noreferrer" : undefined} title={action.label} aria-label={action.label}
            className={fileToolClass}>{action.icon}</a>
        ) : (
          <button key={action.label} type="button" onClick={action.run} disabled={action.disabled}
            title={action.label} aria-label={action.label} className={fileToolClass}>{action.icon}</button>
        ))}
      </div>
      {actions.length > 0 && (
        <DropdownMenu.Root modal={false}>
          <DropdownMenu.Trigger className="file-viewer-more items-center gap-1 rounded-md border border-border px-2 py-1">
            File actions <IconChevronDown size={14} aria-hidden="true" />
          </DropdownMenu.Trigger>
          <DropdownMenu.Content align="end" sideOffset={4} collisionPadding={8}
            onCloseAutoFocus={(event) => {
              const action = afterMenuClose.current;
              if (!action) return;
              afterMenuClose.current = null;
              event.preventDefault();
              action();
            }}
            className="file-viewer-menu z-20 rounded-md border border-border bg-card p-1 shadow-lg">
            {actions.map((action) => "href" in action ? (
              <DropdownMenu.Item key={action.label} asChild>
                <a href={action.href} download={action.download} target={action.target}
                  rel={action.target ? "noreferrer" : undefined}>{action.icon}{action.label}</a>
              </DropdownMenu.Item>
            ) : (
              <DropdownMenu.Item key={action.label} disabled={action.disabled} onSelect={() => {
                if (action.movesFocus) afterMenuClose.current = action.run;
                else action.run();
              }}>
                {action.icon}{action.label}
              </DropdownMenu.Item>
            ))}
          </DropdownMenu.Content>
        </DropdownMenu.Root>
      )}
    </>
  );
}

function FileViewerInner({ tab, failureDraft, setFailureDraft, reload, retained, panelId, active, actions: tabActions, token, onClose,
  readerSize, changeReaderSize }: {
  tab: ReaderAttempt;
  failureDraft: PathDraft | null; setFailureDraft: Dispatch<SetStateAction<PathDraft | null>>;
  reload?: ReaderTab["reload"]; retained: boolean;
  panelId: string; active: boolean; actions: FileViewerTabActions; token: string; onClose: () => void;
  readerSize: ReaderSize; changeReaderSize?: (value: string) => void;
}) {
  const viewer = tab.request;
  const { sessionId, path, source, mode, reloadSource } = viewer;
  const isCurrent = useCallback(() => tabActions.owns(viewer), [tabActions, viewer]);
  const canInteract = useCallback(() => tabActions.canInteract(viewer), [tabActions, viewer]);
  const markSlow = useCallback(() => tabActions.markSlow(viewer), [tabActions, viewer]);
  const retryPending = useCallback(() => tabActions.retryPending(viewer), [tabActions, viewer]);
  const startPositionAction = useCallback((position?: FilePosition | null) => tabActions.locate(viewer, position),
    [tabActions, viewer]);
  const unsupported = mode === "unsupported";
  const resolvesCommand = usesFileResolver(viewer);
  const { opening, phase } = tab;
  const confirmedMeta = opening.confirmed ?? null;
  useEffect(() => {
    if (phase !== "opening") return;
    const timer = window.setTimeout(markSlow, SLOW_FILE_READ_MS);
    return () => window.clearTimeout(timer);
  }, [markSlow, phase]);
  const [confirmedOnStart] = useState(() => !!confirmedMeta);
  const reportFailure = useCallback((failure: FileReadFailure) => tabActions.fail(viewer, failure), [tabActions, viewer]);
  const markReady = useCallback(() => tabActions.ready(viewer), [tabActions, viewer]);
  const [reportedTarget, setCommandTarget] = useState<ShowMeEditTarget | null>(null);
  const [reportedDescription, setDescription] = useState(() => mode === "description" ? path : viewer.description);
  const [canSearchDescription, setCanSearchDescription] = useState(false);
  const outcome = tab.error;
  const clarification = outcome?.clarification ? outcome.message : undefined;
  const error = outcome?.clarification ? undefined : outcome ?? (unsupported ? UNSUPPORTED_LINK : undefined);
  const commandTarget = opening.resolved?.target ?? reportedTarget;
  const description = opening.resolved?.description ?? reportedDescription;
  const position = tab.position ?? undefined;
  const [pathDraft, setPathDraft] = useState(() => mode === "edit" ? fileEditDraft(path, position) : null);
  const draftRetired = pathDraft?.openingRequest && pathDraft.openingRequest !== opening.request;
  const draft = failureDraft ?? (draftRetired ? null : pathDraft);
  const setDraft = failureDraft ? setFailureDraft : setPathDraft;
  const setDropNotice = (notice: boolean) =>
    setDraft((current) => current && current.dropNotice !== notice ? { ...current, dropNotice: notice } : current);
  const [copyStatus, setCopyStatus] = useState("");
  const [markdownView, setMarkdownView] = useState<{ requested: ReaderTab["position"]; mode: "reading" | "source" }>();
  const [pathExpanded, setPathExpanded] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (scroller.current && active) applyReaderSize(scroller.current, readerSize);
  }, [readerSize, active]);
  const [positionResult, setPositionResult] = useState<{ position: FilePosition; note: string | null; footnoteId?: string }>();
  const positioned = useRef<FilePosition | undefined>(undefined);
  const activePosition = active ? position : undefined;
  const reportPosition = useCallback<ReportFilePosition>((note, footnoteId) => {
    if (!activePosition || positioned.current === activePosition || !canInteract()) return false;
    positioned.current = activePosition;
    setPositionResult({ position: activePosition, note, footnoteId });
    return true;
  }, [activePosition, canInteract]);
  const locate = useCallback<LocateInDocument>((target, anchor, invalidLocation) => {
    const nextPosition = target ? { ...target } : null;
    if (!tabActions.locate(viewer, nextPosition, invalidLocation) || invalidLocation) return;
    // Each activation owns one scroll, even when its destination is the same.
    setMarkdownView(undefined);
    if (anchor) anchor.scrollIntoView?.({ block: "start" });
    else if (!target && scroller.current) scroller.current.scrollTop = 0;
  }, [tabActions, viewer]);
  const open = useCallback((target: string, options?: FileViewerOptions) => {
    tabActions.open(viewer, target, options);
  }, [tabActions, viewer]);

  useEffect(() => {
    if (unsupported) {
      tabActions.fail(viewer, UNSUPPORTED_LINK);
      return;
    }
    if (mode === "edit" || confirmedOnStart) return;
    const abort = new AbortController();
    const current = () => !abort.signal.aborted && isCurrent();
    const init = {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
      signal: abort.signal,
    } as const;
    (async () => {
      try {
        // StrictMode can retire an effect before its replacement dispatches.
        await Promise.resolve();
        if (!current()) return;
        if (resolvesCommand) {
          const url = new URL(`/api/sessions/${encodeURIComponent(sessionId)}/showme/resolve`, window.location.origin);
          if (source) url.searchParams.set("source", source);
          if (reloadSource) url.searchParams.set("reload_source", reloadSource);
          const res = await fetch(url, {
            ...init, method: "POST",
            headers: { ...init.headers, "Content-Type": "application/json" },
            body: JSON.stringify(mode === "description" ? { text: path, mode } : { text: path }),
          });
          if (!current()) return;
          if (!res.ok) {
            const failure = await readFileErrorBody(res);
            if (!current()) return;
            if (failure?.description) setDescription(failure.description);
            setCommandTarget(failure?.target ?? null);
            setCanSearchDescription(failure?.can_search_description === true);
            if (failure?.code === "working_directory_unavailable") {
              throw new FileReadFailure(failure.detail, "edit", { denied: true });
            }
            throw fileStatusError(res.status, failure);
          }
          const result = (await res.json()) as ShowMeResolveResponse;
          if (!current()) return;
          if (result.description) setDescription(result.description);
          if ("metadata" in result) {
            setCommandTarget(result.target);
            tabActions.confirm(viewer, result.metadata, { target: result.target, description: result.description });
          } else {
            setCommandTarget(null);
            tabActions.fail(viewer, { message: result.message, clarification: true });
          }
          return;
        }
        const metadata = await readFileMetadata(viewer, init);
        if (current()) tabActions.confirm(viewer, metadata);
      } catch (e) {
        if (current()) reportFailure(readFailure(e));
      }
    })();
    return () => abort.abort();
  }, [viewer, sessionId, path, source, mode, reloadSource, token, unsupported, resolvesCommand, confirmedOnStart, isCurrent, reportFailure, tabActions]);

  const visibleMeta = error && (error.denied || reload) ? null : confirmedMeta;
  const positionCheck = tab.positionCheck;
  const finishPositionCheck = useCallback((note?: string) => {
    if (positionCheck) tabActions.settlePosition(viewer, positionCheck, note);
  }, [tabActions, viewer, positionCheck]);
  const positionTypeNote = visibleMeta && positionNoteFor(positionCheck?.position, visibleMeta.kind);
  useLayoutEffect(() => {
    if (active && positionTypeNote) finishPositionCheck(positionTypeNote);
  }, [active, finishPositionCheck, positionTypeNote]);
  const locatedPosition = positionResult?.position === position ? positionResult : undefined;
  const editablePosition = locatedPosition?.footnoteId ? { kind: "heading" as const, slug: locatedPosition.footnoteId } : position;
  const resolvedPositionNote = visibleMeta ? positionNoteFor(position, visibleMeta.kind) ?? locatedPosition?.note : undefined;
  const positionNote = visibleMeta ? tab.invalidLocation ?? resolvedPositionNote : undefined;
  const positionReport = tab.positionReport;
  useLayoutEffect(() => {
    if (active && phase === "ready" && resolvedPositionNote && positionReport !== undefined) {
      tabActions.reportPositionFailure(viewer, positionReport, resolvedPositionNote);
    }
  }, [active, phase, positionReport, resolvedPositionNote, tabActions, viewer]);
  const positionPending = !!position && positionNote === undefined;
  const showSource = markdownView && markdownView.requested === tab.position
    ? markdownView.mode === "source" : position?.kind === "lines";
  const displayPath = visibleMeta?.absolute_path ?? commandTarget?.path ?? (resolvesCommand && description ? "" : path);
  const filename = (error && reload?.name) || displayPath.split("/").pop() || displayPath ||
    (description ? "Find a file" : "Open file");
  const canonicalUrl = visibleMeta && confirmedFileUrl(viewer, visibleMeta);
  const bytesUrl = canonicalUrl && `${canonicalUrl}&token=${encodeURIComponent(token)}`;
  const previewUrl = bytesUrl && (visibleMeta?.kind === "pdf" ? `${bytesUrl}&preview=true` : bytesUrl);
  const search = (text: string) => open(text, { mode: "description" });
  const retry = () => tabActions.reload(viewer);
  const choose = () => tabActions.showPicker(viewer);
  const edit = () => {
    if (!tabActions.canEditPath(viewer)) return;
    const target = resolvesCommand && description && !confirmedMeta && !commandTarget ? ""
      : editablePath(viewer, visibleMeta ?? (resolvesCommand && !commandTarget ? confirmedMeta : null), commandTarget);
    const openingRequest = phase === "ready" ? undefined : opening.request;
    const draftPosition = tab.position === undefined ? commandTarget?.position ?? undefined : editablePosition;
    setPathDraft(fileEditDraft(target, draftPosition, { openingRequest }));
  };
  const updateDraft = (field: "path" | "position", value: string) => {
    setDraft((current) => current && current[field] !== value ? { ...current, [field]: value } : current);
  };
  const replacePathSelection = (field: HTMLTextAreaElement, replacement: string) => {
    const { selectionStart, selectionEnd, selectionDirection } = field;
    if (replacement && selectionStart === selectionEnd && field.value.length) {
      // End Chromium's pending typing transaction before inserting a paste.
      const adjacent = selectionStart > 0 ? selectionStart - 1 : 1;
      field.setSelectionRange(adjacent, adjacent);
      field.setSelectionRange(selectionStart, selectionEnd, selectionDirection);
    }
    // The native editing command records the change in the browser's undo history.
    if (!field.ownerDocument.execCommand?.(replacement ? "insertText" : "delete", false, replacement)) {
      field.setRangeText(replacement, selectionStart, selectionEnd, "end");
    }
    updateDraft("path", field.value);
  };
  const copyPathSelection = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const field = event.currentTarget;
    const { start, end } = pathTokenSelection(field.value, field.selectionStart, field.selectionEnd);
    const selection = field.value.slice(start, end);
    const literal = unescapeEditedPath(selection);
    if (literal === selection) return;
    event.preventDefault();
    event.clipboardData.setData("text/plain", literal);
    field.setSelectionRange(start, end, field.selectionDirection);
    if (event.type === "cut") replacePathSelection(field, "");
  };
  const pastePath = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const text = event.clipboardData.getData("text/plain");
    if (!text) return;
    setDropNotice(false);
    const field = event.currentTarget;
    const { start, end } = pathTokenSelection(field.value, field.selectionStart, field.selectionEnd);
    const insertion = escapePathForEditing(text);
    if (insertion === text && start === field.selectionStart && end === field.selectionEnd) return;
    event.preventDefault();
    field.setSelectionRange(start, end, field.selectionDirection);
    replacePathSelection(field, insertion);
  };
  const startPathDrag = (event: DragEvent<HTMLTextAreaElement>) => {
    const field = event.currentTarget;
    const { start, end } = pathTokenSelection(field.value, field.selectionStart, field.selectionEnd);
    if (start !== field.selectionStart || end !== field.selectionEnd) {
      event.preventDefault();
      setDropNotice(true);
    }
  };
  const dropPath = (event: DragEvent<HTMLTextAreaElement>) => {
    const text = event.dataTransfer.getData("text/plain");
    // Native drops can split an escape token or normalize CR. Cancel before
    // the browser makes an edit that would discard its existing redo history.
    if (event.currentTarget.value.includes("␛") || escapePathForEditing(text) !== text) {
      event.preventDefault();
      setDropNotice(true);
    }
  };
  const copy = async (command = false) => {
    if (!visibleMeta || !canInteract()) return;
    const absolute = visibleMeta.absolute_path;
    const suffix = positionNote === null ? filePositionSuffix(editablePosition) : "";
    const text = command ? `/showme ${JSON.stringify(absolute)} --source ${JSON.stringify(visibleMeta.source)}${suffix}` : absolute;
    try {
      await navigator.clipboard.writeText(text);
      if (canInteract()) setCopyStatus(command ? "Command copied." : "Path copied.");
    } catch {
      if (canInteract()) setCopyStatus(command ? "Couldn't copy the command." : "Couldn't copy the file path.");
    }
  };

  const literalDraftPath = draft ? unescapeEditedPath(draft.path) : "";
  const unsupportedDraft = draft?.failure ? draft.failure.request.mode === "unsupported" : unsupported;
  const canSubmitDraft = draft !== null && !!literalDraftPath.trim() && (!unsupportedDraft || literalDraftPath !== draft.initialPath);
  const actions: FileAction[] = [];
  if (!retained && draft === null && error?.recovery !== "edit" && (!resolvesCommand || confirmedMeta || error || clarification)) {
    actions.push({ label: "Edit path", icon: <IconFile size={16} />, run: edit, movesFocus: true });
  }
  if (visibleMeta) {
    actions.push(
      { label: "Copy path", icon: <IconCopy size={16} />, run: () => void copy() },
      { label: "Copy /showme command", icon: <IconTerminal2 size={16} />, run: () => void copy(true), disabled: positionPending && !error },
    );
  }
  if (phase === "ready") {
    actions.push({ label: "Reload file", icon: <IconRefresh size={16} />, run: retry, disabled: draft !== null });
  } else if (phase === "opening" && confirmedMeta) {
    actions.push({ label: "Retry file", icon: <IconRefresh size={16} />, run: retryPending, disabled: draft !== null });
  }
  if (previewUrl && visibleMeta?.kind === "pdf") {
    actions.push({ label: "Open in new tab", icon: <IconExternalLink size={16} />, href: previewUrl, target: "_blank" });
  }
  if (bytesUrl) {
    actions.push({ label: "Download file", icon: <IconDownload size={16} />, href: bytesUrl, download: filename });
  }
  const textFile = visibleMeta && visibleMeta.kind !== "image" && visibleMeta.kind !== "pdf";
  return (
    <>
      <header className="file-viewer-header border-b border-border">
        <div className="file-viewer-heading">
          <IconFile size={18} className="text-muted-foreground shrink-0" />
          {active ? <DialogPrimitive.Title data-description-heading tabIndex={-1} title={filename}
            className="min-w-0 flex-1 truncate font-semibold text-foreground">{filename}</DialogPrimitive.Title>
            : <h2 title={filename} className="min-w-0 flex-1 truncate font-semibold text-foreground">{filename}</h2>}
          {visibleMeta && <span className="file-viewer-bytes shrink-0 text-muted-foreground">
            {formatBytes(visibleMeta.size)}
          </span>}
        </div>
        <button type="button" className={`file-viewer-close ${fileToolClass}`} onClick={onClose} title="Close" aria-label="Close">
          <IconX size={18} />
        </button>
        <div className="file-viewer-tools">
          {textFile && <label className="flex items-center gap-1.5">
            Text size
            <select className="min-h-8 rounded-md border border-border bg-background px-1"
              value={readerSize} onChange={(event) => {
                if (canInteract()) changeReaderSize?.(event.currentTarget.value);
              }} disabled={!changeReaderSize}>
              {Object.entries(READER_SIZES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>}
          <FileActions actions={actions} />
        </div>
        {displayPath && displayPath !== filename && (
          <div className="file-viewer-path text-muted-foreground">
            <button type="button" aria-expanded={pathExpanded} onClick={() => setPathExpanded(!pathExpanded)}
              aria-label="Full file path" title="Full file path" className="shrink-0 rounded-md p-1 hover:bg-accent">
              <IconChevronDown size={16} className={pathExpanded ? "rotate-180" : undefined} />
            </button>
            <div className={pathExpanded ? "file-viewer-path-expanded" : "truncate"} tabIndex={pathExpanded ? 0 : undefined}>
              {displayPath}
            </div>
          </div>
        )}
        {mode === "command" && !description && visibleMeta && path !== displayPath && (
          <div className="file-viewer-request truncate text-muted-foreground" title={path}>{path}</div>
        )}
        {description && <div className="file-viewer-request truncate text-muted-foreground" title={description}>{description}</div>}
      </header>

      {copyStatus && <div role="status" className="px-5 py-1 text-sm text-muted-foreground">{copyStatus}</div>}
      {visibleMeta?.kind === "markdown" && !error && (
        <div role="group" aria-label="Markdown view" className="flex gap-1 border-b border-border px-5 py-1.5 text-sm">
          <button type="button" aria-pressed={!showSource} disabled={positionPending}
            className="rounded px-3 py-1 aria-pressed:bg-accent aria-pressed:font-semibold"
            onClick={() => setMarkdownView({ requested: tab.position, mode: "reading" })}>Reading</button>
          <button type="button" aria-pressed={showSource} disabled={positionPending}
            className="rounded px-3 py-1 aria-pressed:bg-accent aria-pressed:font-semibold"
            onClick={() => setMarkdownView({ requested: tab.position, mode: "source" })}>Source</button>
        </div>
      )}
      {(!retained || failureDraft) && draft !== null && (
        <form className="flex flex-wrap items-end gap-2 border-b border-border p-3" onSubmit={(event) => {
          event.preventDefault();
          if (canSubmitDraft && (draft.failure ? canInteract() : tabActions.canEditPath(viewer))) {
            const editedPosition = parseFilePosition(draft.position.trim()) ?? null;
            const samePath = literalDraftPath === draft.initialPath;
            const failure = draft.failure;
            const editedRequest = failure?.request ?? viewer;
            const editedOpening = failure ?? opening;
            const editedFile = editedOpening.confirmed ?? editedOpening.previousTarget;
            // A command can resolve a relative alias in one source to a file
            // in another. A position-only edit rechecks that confirmed file.
            const confirmedTarget = samePath && usesFileResolver(editedOpening.request)
              ? editedFile?.absolute_path : undefined;
            const target = confirmedTarget ?? (literalDraftPath.startsWith("~") ? `./${literalDraftPath}` : literalDraftPath);
            const absolute = target.startsWith("/");
            const currentFile = !failure && phase === "ready" ? confirmedMeta : null;
            const boundSource = editedFile?.source ?? (failure ? undefined : commandTarget?.source) ?? editedRequest.source;
            const selectedSource = !samePath && absolute ? sourceContainingPath(boundSource, filePathSegments(target)) : boundSource;
            const optionsRequest = samePath && failure ? failedFileRead(failure) : editedRequest;
            const options = { source: selectedSource,
              sourceDirectory: absolute ? undefined : currentFile?.canonical_directory ?? optionsRequest.sourceDirectory,
              position: editedPosition, description: failure ? optionsRequest.description : description };
            if (failure) tabActions.editFailure(viewer, failure, target, options);
            else tabActions.edit(viewer, target, options, samePath);
            setDraft(null);
          }
        }}>
          <div className="flex min-w-44 flex-1 flex-col gap-1 text-sm">
            <label htmlFor={`${panelId}-path-draft`}>File path</label>
            <textarea id={`${panelId}-path-draft`} ref={mode === "edit" ? undefined : focusTextarea}
              rows={2} className="rounded-md border border-border bg-background px-2 py-1 font-mono"
              value={draft.path}
              aria-describedby={draft.path.includes("␛") ? `${panelId}-path-escapes` : undefined}
              onChange={(event) => {
                updateDraft("path", event.currentTarget.value);
                setDropNotice(false);
              }}
              onCopy={copyPathSelection}
              onCut={copyPathSelection}
              onPaste={pastePath}
              onDragStart={startPathDrag}
              onDrop={dropPath} />
            {draft.path.includes("␛") && <span id={`${panelId}-path-escapes`} className="text-xs text-muted-foreground">
              ␛r represents a carriage return. Enter ␛␛ for a literal ␛.
            </span>}
            {draft.dropNotice && <span role="status" className="text-xs text-muted-foreground">
              Use copy or cut, then paste to place this text.
            </span>}
          </div>
          <label className="flex flex-col gap-1 text-sm">
            Position (optional)
            <input className="rounded-md border border-border bg-background px-2 py-1 font-mono"
              value={draft.position} placeholder=":12, #L12-L18, #heading"
              onChange={(event) => updateDraft("position", event.currentTarget.value)} />
          </label>
          <button type="submit" disabled={!canSubmitDraft} className="rounded-md bg-primary px-3 py-1 text-primary-foreground disabled:opacity-50">Open file</button>
          {mode !== "edit" && <button type="button" className="px-2 py-1 text-sm" onClick={() => setDraft(null)}>Cancel</button>}
        </form>
      )}
      <div ref={scroller} className="file-viewer-body flex-1 min-h-0 min-w-0 overflow-auto bg-background">
        {description && (clarification || error) && (
          <DescriptionForm description={description} clarification={clarification} search={search} choose={choose} />
        )}
        {error ? (
          <FileError failure={error} compact={!!description}
            originalPdf={error.originalPdf && reload ?
              <OriginalPdfLinks request={viewer} meta={error.originalPdf} token={token} /> : undefined}
            retry={!resolvesCommand || !description || confirmedMeta ? retry : undefined}
            edit={!retained && draft === null ? edit : undefined} choose={choose}
            search={canSearchDescription ? () => search(path) : undefined} />
        ) : mode === "edit" || clarification ? null : !visibleMeta || !canonicalUrl || !previewUrl ? (
          tab.slow ? <SlowFileLoading onRetry={canRetryPendingFile(tab) ? retryPending : undefined}>The file has not finished loading.</SlowFileLoading>
            : <FileLoading>{mode === "description" ? "Searching the conversation…"
            : mode === "command" ? "Loading file or searching the conversation…" : "Loading…"}</FileLoading>
        ) : (
          <FileBody
            meta={visibleMeta}
            contentUrl={canonicalUrl}
            token={token}
            sessionId={sessionId}
            panelId={panelId}
            isCurrent={isCurrent}
            canInteract={canInteract}
            position={phase === "ready" ? position : undefined}
            positionCheck={active && !positionTypeNote ? positionCheck?.position : undefined}
            finishPositionCheck={finishPositionCheck}
            onPosition={reportPosition}
            onLocate={locate}
            startPositionAction={startPositionAction}
            showSource={phase === "ready" && showSource}
            onOpen={open}
            onReady={markReady}
            onError={reportFailure}
            active={active}
            onRetry={retryPending}
            slow={tab.slow}
          />
        )}
      </div>
    </>
  );
}

function DescriptionForm({ description, clarification, search, choose }: {
  description: string; clarification?: string; search: (text: string) => void; choose: () => void;
}) {
  const [draft, setDraft] = useState(description);
  return (
    <form className="flex flex-col gap-2 border-b border-border px-5 py-3 text-sm" onSubmit={(event) => {
      event.preventDefault();
      if (draft.trim()) search(draft);
    }}>
      {clarification && <p role="status">{clarification}</p>}
      <label className="flex flex-col gap-1">
        File description
        <textarea rows={2} ref={clarification ? focusTextarea : undefined}
          className="rounded-md border border-border bg-background px-2 py-1"
          value={draft} onChange={(event) => setDraft(event.currentTarget.value)} />
      </label>
      <div className="flex flex-wrap items-center gap-4">
        <button type="submit" disabled={!draft.trim()} className="rounded-md bg-primary px-3 py-1 text-primary-foreground disabled:opacity-50">Search conversation</button>
        {clarification && <button type="button" className="underline" onClick={choose}>Choose a file</button>}
      </div>
    </form>
  );
}

function FileLoading({ children = "Loading…" }: { children?: string }) {
  return (
    <div role="status" className="flex h-full items-center justify-center text-sm text-muted-foreground">
      {children}
    </div>
  );
}

function FileError({ failure, retry, edit, choose, search, compact, originalPdf }: {
  failure: OpenError; retry?: () => void; edit?: () => void; choose: () => void; search?: () => void; compact: boolean;
  originalPdf?: ReactNode;
}) {
  return (
    <div className={cn("flex items-center justify-center px-6", compact ? "py-4" : "h-full")}>
      <div className="max-w-md rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
        <div className="font-semibold mb-1">Couldn't open file</div>
        <div role="alert" className="whitespace-pre-wrap break-words">{failure.message}</div>
        <div className="mt-2 flex flex-wrap gap-4">
          {retry && (failure.recovery === "retry" || failure.recovery === "reconfirm") &&
            <button type="button" onClick={retry} className="underline">Retry</button>}
          {failure.recovery === "edit" && edit && <button type="button" onClick={edit} className="underline">Edit path</button>}
          <button type="button" onClick={choose} className="underline">Choose a file</button>
          {search && <button type="button" onClick={search} className="underline">Search by description</button>}
          {originalPdf}
        </div>
      </div>
    </div>
  );
}

interface BodyProps {
  meta: FileViewerMetadata;
  contentUrl: string;
  token: string;
  isCurrent: () => boolean;
  onReady: () => void;
  onError: (error: FileReadFailure) => void;
}

type LocateInDocument = (position?: FilePosition, anchor?: HTMLElement, invalidLocation?: string) => void;

interface TextBodyProps extends BodyProps {
  sessionId: string;
  panelId: string;
  position?: FilePosition;
  positionCheck?: FilePosition;
  finishPositionCheck: (note?: string) => void;
  canInteract: () => boolean;
  onOpen: (path: string, options?: FileViewerOptions) => void;
  onPosition: ReportFilePosition;
  onLocate: LocateInDocument;
  startPositionAction: () => boolean;
  showSource: boolean;
}

interface MediaLoadingProps {
  onRetry: () => void;
  slow?: boolean;
}

function FileBody({ active, onRetry, slow, ...props }: TextBodyProps & MediaLoadingProps & { active: boolean }) {
  const { meta } = props;
  if (meta.kind === "image") return <ImageBody {...props} onReaderAction={props.startPositionAction} onRetry={onRetry} slow={slow} />;
  if (meta.kind === "pdf") return <PdfBody {...props} active={active} onReaderAction={props.startPositionAction}
    onRetry={onRetry} slow={slow} />;
  return <TextBody {...props} onRetry={onRetry} slow={slow} />;
}

async function readImage(response: Response, signal: AbortSignal): Promise<string> {
  const image = await response.blob();
  signal.throwIfAborted();
  if (image.type !== "image/svg+xml") return URL.createObjectURL(image);
  // A data URL stays opaque if opened as a document from the image menu.
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    const abort = () => reader.abort();
    reader.onloadend = () => {
      signal.removeEventListener("abort", abort);
      if (signal.aborted) reject(signal.reason);
      else if (typeof reader.result === "string") resolve(reader.result);
      else reject(new FileReadFailure("Couldn't load the preview.", "retry"));
    };
    signal.addEventListener("abort", abort, { once: true });
    reader.readAsDataURL(image);
  });
}

function releaseImage(url: string) {
  if (url.startsWith("blob:")) URL.revokeObjectURL(url);
}
const readText = (response: Response) => response.text();
const readPdf = (response: Response) => response.arrayBuffer();

function SlowFileLoading({ children, onRetry }: { children: ReactNode; onRetry?: () => void }) {
  return <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-sm text-muted-foreground">
    <p role="status">{children}</p>
    {onRetry && <button type="button" className="underline" onClick={onRetry}>Retry</button>}
  </div>;
}

function MediaLoading({ kind, onRetry, slow }: MediaLoadingProps & { kind: "image" | "PDF" }) {
  return slow ? <SlowFileLoading onRetry={onRetry}>The {kind} preview has not finished loading.</SlowFileLoading> : <FileLoading />;
}

function ImageBody({ onRetry, slow, onReaderAction, ...props }: BodyProps & MediaLoadingProps & { onReaderAction: () => boolean }) {
  const { meta, onError, onReady, isCurrent } = props;
  const imageUrl = useFileContent(props, readImage, releaseImage);
  const [zoomed, setZoomed] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const fail = (image: HTMLImageElement) => {
    if (image.isConnected && isCurrent()) onError(new FileReadFailure("Couldn't load the preview.", "retry"));
  };
  return (
    <div className="flex h-full items-center justify-center p-6" aria-busy={!loaded}>
      {!loaded && <MediaLoading kind="image" onRetry={onRetry} slow={slow} />}
      {imageUrl && (
        <button
          type="button"
          onClick={() => { if (onReaderAction()) setZoomed((z) => !z); }}
          hidden={!loaded}
          className="group relative max-h-full max-w-full focus:outline-none"
          aria-label={zoomed ? "Zoom out" : "Zoom in"}
        >
          <img
            src={imageUrl}
            alt={meta.path}
            onLoad={async ({ currentTarget: image }) => {
              try {
                await image.decode();
                if (!image.isConnected || !isCurrent()) return;
                setLoaded(true);
                onReady();
              } catch {
                fail(image);
              }
            }}
            onError={(event) => fail(event.currentTarget)}
            className={cn(
              "block rounded-md shadow-md transition-transform",
              zoomed ? "max-h-none max-w-none cursor-zoom-out" : "max-h-[70vh] max-w-full object-contain cursor-zoom-in"
            )}
          />
          <span className="absolute right-2 top-2 flex items-center justify-center size-7 rounded-full bg-black/50 text-white opacity-0 transition-opacity group-hover:opacity-100">
            {zoomed ? <IconZoomOut size={14} /> : <IconZoomIn size={14} />}
          </span>
        </button>
      )}
    </div>
  );
}

type PdfBodyProps = BodyProps & MediaLoadingProps & { active: boolean; onReaderAction: () => boolean };

function PdfBody({ active, onReaderAction, onRetry, slow, ...props }: PdfBodyProps) {
  const { meta, onError, onReady, isCurrent } = props;
  const [frameDocument, setFrameDocument] = useState<HTMLDocument>();
  const [loaded, setLoaded] = useState(false);
  const interactive = active && loaded;
  const attachFrame = useCallback((frame: HTMLIFrameElement | null) => {
    const document = frame?.contentDocument;
    if (document?.body) setFrameDocument(document);
  }, []);
  const ready = useCallback(() => {
    if (!isCurrent()) return;
    setLoaded(true);
    onReady();
  }, [isCurrent, onReady]);
  const fail = useCallback((message: string) => {
    if (isCurrent()) onError(new FileReadFailure(message, "retry", { originalPdf: meta }));
  }, [isCurrent, meta, onError]);
  const failRead = useCallback((failure: FileReadFailure) => {
    if (failure.recovery === "retry") fail(failure.message);
    else onError(failure);
  }, [fail, onError]);
  const bytes = useFileContent({ ...props, onError: failRead, contentUrl: `${props.contentUrl}&preview=true` }, readPdf);
  return (
    <div className="relative h-full" aria-busy={!loaded}>
      {!loaded && <div className="absolute inset-0 z-10 bg-background">
        <MediaLoading kind="PDF" onRetry={onRetry} slow={slow} />
      </div>}
      <iframe
        ref={attachFrame}
        title="PDF preview"
        className="block h-full w-full border-0 bg-white"
        inert={!interactive}
        tabIndex={interactive ? 0 : -1}
        aria-hidden={!interactive || undefined}
        onLoad={(event) => attachFrame(event.currentTarget)}
      />
      {bytes && frameDocument && createPortal(
        <PdfReaderFrame bytes={bytes} ownerDocument={frameDocument} interactive={interactive}
          onReady={ready} onError={fail} onReaderAction={onReaderAction} />,
        frameDocument.body,
      )}
    </div>
  );
}

function useFileContent<T>(
  { contentUrl, token, isCurrent, onError }: BodyProps,
  read: (response: Response, signal: AbortSignal) => Promise<T>,
  release?: (body: T) => void,
) {
  const [body, setBody] = useState<T | null>(null);

  useEffect(() => {
    const abort = new AbortController();
    const current = () => !abort.signal.aborted && isCurrent();
    let retained: T | null = null;
    (async () => {
      try {
        await Promise.resolve();
        if (!current()) return;
        const res = await fetch(contentUrl, {
          headers: { Authorization: `Bearer ${token}` },
          cache: "no-store",
          signal: abort.signal,
        });
        if (!current()) return;
        if (!res.ok) throw await fileReadError(res);
        const content = await read(res, abort.signal);
        if (current()) {
          retained = content;
          setBody(content);
        } else {
          release?.(content);
        }
      } catch (e) {
        if (current()) onError(readFailure(e));
      }
    })();
    return () => {
      abort.abort();
      if (retained !== null) release?.(retained);
    };
  }, [contentUrl, token, isCurrent, onError, read, release]);
  return body;
}

const TextBody = memo(function TextBody(props: TextBodyProps & MediaLoadingProps) {
  const { meta, sessionId, panelId, token, position, positionCheck, finishPositionCheck, canInteract,
    onReady, onOpen, onPosition, onLocate, startPositionAction, showSource, onRetry, slow } = props;
  const text = useFileContent(props, readText);
  const [section, setSection] = useState<{ position?: FilePosition; start: number }>();
  useLayoutEffect(() => {
    // Commit the ready tab before its newly rendered links can receive input.
    if (text !== null) onReady();
  }, [text, onReady]);
  useLayoutEffect(() => {
    if (text !== null && positionCheck?.kind === "lines") {
      finishPositionCheck(linePositionNote(text, positionCheck));
    }
  }, [text, positionCheck, finishPositionCheck]);
  if (text === null) return slow
    ? <SlowFileLoading onRetry={onRetry}>The file has not finished loading.</SlowFileLoading> : <FileLoading />;
  const markdown = meta.kind === "markdown";
  const headingCheck = positionCheck?.kind === "heading" ? positionCheck.slug : undefined;
  const renderMarkdown = markdown && (!showSource || headingCheck !== undefined);
  const language = meta.kind === "markdown" ? "markdown" : meta.kind === "code" ? languageHint(meta.path) : null;
  return <>
    {renderMarkdown && <MarkdownBody text={text} meta={meta} sessionId={sessionId} panelId={panelId} token={token}
      hidden={showSource} headingCheck={headingCheck} finishPositionCheck={finishPositionCheck}
      canInteract={canInteract} position={showSource ? undefined : position} onPosition={onPosition} onOpen={onOpen} onLocate={onLocate} />}
    {(!markdown || showSource) && <FileSourceView raw={text} highlight={meta.kind !== "text"} language={language}
      position={position?.kind === "lines" ? position : undefined} onPosition={onPosition}
      sectionStart={section?.position === position ? section?.start : undefined}
      onSectionChange={(start) => {
        if (startPositionAction()) setSection({ position, start });
      }} />}
  </>;
});

/**
 * Custom renderer for fenced code blocks inside markdown bodies.
 *
 * react-markdown calls our `pre` renderer for ```fenced blocks. The
 * standard inline `<code>` (without surrounding <pre>) is left alone —
 * we only enhance block-level code. Inside the pre we expect a single
 * <code class="language-xxx"> child (this is how rehype-highlight emits).
 *
 * We add two affordances GitHub gets right and we want here:
 *   - A language badge in the top-right showing the parsed language.
 *   - A copy-to-clipboard button next to it (icon-only, hover-revealed
 *     on desktop, always visible on touch).
 *
 * We extract the language and the raw code text from the child <code>
 * element so the copy button puts the *unhighlighted* source on the
 * clipboard (not the syntax-highlighted span soup).
 */
function MarkdownPreBlock({ children, className, ...rest }: HTMLAttributes<HTMLPreElement>) {
  const [copied, setCopied] = useState(false);

  // Drill into the <code> child to extract language + raw text. children
  // from react-markdown is normally a single React element; we defend
  // against null/array shapes.
  const code = Array.isArray(children) ? children[0] : children;
  let language: string | null = null;
  let rawText = "";
  if (
    code &&
    typeof code === "object" &&
    "props" in code &&
    code.props
  ) {
    const codeProps = code.props as { className?: string; children?: ReactNode };
    const m = (codeProps.className ?? "").match(/language-([\w-]+)/);
    language = m ? m[1] : null;
    rawText = extractText(codeProps.children);
  }

  const copy = async () => {
    if (!rawText) return;
    try {
      await navigator.clipboard.writeText(rawText);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      // Old browsers w/o clipboard API: ignore silently.
    }
  };

  return (
    <div className="md-codeblock group relative my-4">
      <div className="md-codeblock-toolbar absolute right-2 top-2 flex items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
        {language && (
          <span className="md-codeblock-lang inline-flex items-center rounded border border-border bg-card/90 px-1.5 py-0.5 text-[10px] font-mono uppercase tracking-wider text-muted-foreground">
            {language}
          </span>
        )}
        <button
          type="button"
          onClick={copy}
          aria-label={copied ? "Copied" : "Copy code"}
          title={copied ? "Copied" : "Copy"}
          className="md-codeblock-copy inline-flex items-center justify-center size-6 rounded border border-border bg-card/90 text-muted-foreground hover:text-foreground hover:bg-card transition-colors"
        >
          {copied ? <IconCheck size={12} /> : <IconCopy size={12} />}
        </button>
      </div>
      <pre className={className} {...rest} tabIndex={0}>
        {children}
      </pre>
    </div>
  );
}

/** Flatten react-markdown's `children` into a plain string suitable
 *  for clipboard. Handles strings, arrays, and elements with their
 *  own children (e.g. highlight.js spans). */
function extractText(node: ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(extractText).join("");
  if (typeof node === "object" && "props" in node && node.props) {
    return extractText((node.props as { children?: ReactNode }).children);
  }
  return "";
}

const HEADING_PREFIX = "file-viewer-heading-";
const MISSING_HEADING = "The requested heading wasn't found.";

function relativeImageReference(url: string | undefined) {
  if (!url || url.startsWith("/") || /^[a-z][a-z\d+.-]*:/i.test(url)) return;
  return splitFileLink(url);
}

function documentAnchor(article: HTMLElement | null, id: string): HTMLElement | null {
  if (!article) return null;
  return Array.from(article.querySelectorAll<HTMLElement>("[id]")).find((node) => node.id === id) ?? null;
}

function documentPositionAnchor(article: HTMLElement | null, slug: string, prefix: string) {
  const heading = documentAnchor(article, prefix + HEADING_PREFIX + slug);
  if (heading || !article) return heading;
  const footnotes = Array.from(article.querySelectorAll<HTMLElement>("[data-footnotes] [id], [data-footnote-ref][id]"));
  // Use the same URI normalization as the GFM renderer after honoring literal IDs.
  const canonicalId = normalizeUri(slug);
  const exact = footnotes.find((node) => node.id === prefix + slug) ?? footnotes.find((node) => node.id === prefix + canonicalId);
  if (exact) return exact;
  const decodedMatches = footnotes.filter((node) => decodeFileReference(node.id.slice(prefix.length)) === slug);
  return decodedMatches.length === 1 ? decodedMatches[0] : undefined;
}

function headingPositionNote(article: HTMLElement | null, slug: string, prefix: string) {
  return documentPositionAnchor(article, slug, prefix) ? undefined : MISSING_HEADING;
}

interface MarkdownBodyProps {
  text: string;
  meta: FileViewerMetadata;
  sessionId: string;
  panelId: string;
  token: string;
  hidden: boolean;
  headingCheck?: string;
  finishPositionCheck: (note?: string) => void;
  canInteract: () => boolean;
  position?: FilePosition;
  onOpen: (path: string, options?: FileViewerOptions) => void;
  onPosition: ReportFilePosition;
  onLocate: LocateInDocument;
}

function MarkdownBody({ text, meta, sessionId, panelId, token, hidden, headingCheck,
  finishPositionCheck, canInteract, position, onOpen, onPosition, onLocate }: MarkdownBodyProps) {
  const article = useRef<HTMLElement>(null);
  const { source, canonical_directory: directory } = meta;
  const prefix = `${panelId}-`;
  const footnoteLabel = `${prefix}footnote-label`;
  useLayoutEffect(() => {
    if (headingCheck !== undefined) finishPositionCheck(headingPositionNote(article.current, headingCheck, prefix));
  }, [headingCheck, finishPositionCheck, prefix]);
  useLayoutEffect(() => {
    if (position?.kind !== "heading") return;
    const anchor = documentPositionAnchor(article.current, position.slug, prefix);
    if (!anchor) {
      onPosition(MISSING_HEADING);
      return;
    }
    const id = anchor.id.slice(prefix.length);
    const footnoteId = id.startsWith(HEADING_PREFIX) ? undefined : id;
    if (onPosition(null, footnoteId)) anchor.scrollIntoView?.({ block: "start" });
  }, [position, onPosition, prefix]);

  const components = useMemo<Components>(() => {
    const locate = (position?: FilePosition) => {
      const note = positionNoteFor(position, "markdown") ??
        (position?.kind === "heading" ? headingPositionNote(article.current, position.slug, prefix)
          : position?.kind === "lines" ? linePositionNote(text, position) : undefined);
      onLocate(position, undefined, note);
    };
    const openReference = ({ path, position, unsupported }: FileReference) => {
      if (!canInteract()) return;
      const absolute = path.startsWith("/");
      // Preserve the full path for the server, including any symlink-sensitive
      // "..". Only a direct reference to the confirmed document can skip I/O.
      const targetSegments = filePathSegments(path);
      const targetPath = targetSegments.join("/");
      const sameFile = absolute ? `/${targetPath}` === meta.absolute_path : targetPath === meta.path.split("/").at(-1);
      if (!unsupported && sameFile) {
        locate(position);
        return;
      }
      onOpen(path, {
        position, source: absolute ? sourceContainingPath(source, targetSegments) : source,
        sourceDirectory: absolute ? undefined : directory,
        mode: unsupported ? "unsupported" : undefined,
      });
    };
    return {
      pre: MarkdownPreBlock,
      h2: ({ node, id, ...props }) => {
        void node;
        return <h2 {...props} id={id === "footnote-label" ? footnoteLabel : id} />;
      },
      table: ({ node, children, ...props }) => {
        void node;
        return <div className="file-markdown-table" tabIndex={0} role="region" aria-label="Table">
          <table {...props}>{children}</table>
        </div>;
      },
      a: ({ node, href, ...props }) => {
        const reference = readFileReference(node);
        if (reference) return <FileReferenceButton position={reference.position} title={props.title}
          onOpen={() => openReference(reference)}>{props.children}</FileReferenceButton>;
        if (!href?.startsWith("#")) return <a {...props} href={href} />;
        return <a {...props} href={href}
          aria-describedby={props["aria-describedby"] === "footnote-label" ? footnoteLabel : props["aria-describedby"]}
          onClick={(event) => {
          event.preventDefault();
          if (!canInteract()) return;
          const anchor = event.currentTarget;
          const footnote = anchor.hasAttribute("data-footnote-ref") || anchor.hasAttribute("data-footnote-backref");
          if (footnote) {
            // GFM footnote IDs contain literal percent escapes for non-ASCII labels.
            const destination = documentAnchor(article.current, href.slice(1));
            if (destination) onLocate(undefined, destination);
            else onLocate(undefined, undefined, "The requested footnote wasn't found.");
          } else {
            locate(parseFilePosition(href, true));
          }
        }} />;
      },
      img: ({ node, src, alt, ...props }) => {
        void node;
        if (hidden) return null;
        const reference = relativeImageReference(src);
        if (!reference) return <img {...props} src={src || undefined} alt={alt} />;
        const target = decodeFileReference(reference.path);
        if (!target) return <img {...props} alt={alt} />;
        const bytes = `${fileUrl(sessionId, target, { source, sourceDirectory: directory })}&token=${encodeURIComponent(token)}`;
        const fragment = reference.fragment === undefined ? "" : `#${reference.fragment}`;
        return <img {...props} src={bytes + fragment} alt={alt} />;
      },
    };
  }, [directory, meta.absolute_path, meta.path, source, sessionId, token, canInteract, hidden, footnoteLabel, onOpen, onLocate, prefix, text]);

  const content = useMemo(() => (
    <Markdown
      urlTransform={documentLinkUrl}
      remarkPlugins={[remarkGfm, remarkMath]}
      remarkRehypeOptions={{ clobberPrefix: `${prefix}user-content-` }}
      rehypePlugins={[
        [rehypeSlug, { prefix: prefix + HEADING_PREFIX }],
        [rehypeHighlight, { ignoreMissing: true }],
        rehypeKatex,
      ]}
      components={components}
    >
      {text}
    </Markdown>
  ), [components, prefix, text]);
  return <article ref={article} hidden={hidden} className="markdown md-github prose">{content}</article>;
}
