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

import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ClipboardEvent, DragEvent, KeyboardEvent, ReactNode, HTMLAttributes } from "react";
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
import { documentLinkUrl, FileReferenceButton, readFileReference, type FileReference } from "./MarkdownFileLink";
import { FileSourceView } from "./FileSourceView";
import { FilePicker } from "./FilePicker";
import { PdfReaderFrame } from "./PdfReaderFrame";
import { applyReaderSize, type ReportFilePosition } from "./fileReaderScroll";
import {
  confirmedOpeningFile, displayedReaderTab, displayedRecovery, failedFileRead, openingDescription, pendingFileRetry,
  readerTabName, retryFileRead, selectableReaderTab, useFileViewerTabs, usesFileResolver,
  type FileViewerRequest, type FileViewerTabActions, type OpenError, type Opening, type OpeningFailure, type ReaderAttempt, type ReaderTab,
} from "./fileViewerTabs";

interface FileUrlOptions {
  metadata?: boolean;
  source?: string;
  sourceDirectory?: FileResourceDirectory;
  canonicalPathId?: string;
}

function fileUrl(
  sessionId: string,
  path: string,
  { metadata = false, source, sourceDirectory, canonicalPathId }: FileUrlOptions = {},
): string {
  const endpoint = metadata ? "files/meta" : "files";
  const query = new URLSearchParams({ path });
  // Use a fresh cache key for byte URLs issued before responses used no-store.
  if (!metadata) query.set("viewer", "1");
  if (source) query.set("source", source);
  if (canonicalPathId) query.set("canonical_path_id", canonicalPathId);
  if (sourceDirectory) {
    query.set("source_directory", sourceDirectory.path);
    query.set("source_directory_id", sourceDirectory.path_id);
  }
  return `${window.location.origin}/api/sessions/${encodeURIComponent(sessionId)}/${endpoint}?${query}`;
}

class FileReadFailure extends Error {
  readonly recovery: "edit" | "retry";
  readonly denied: boolean;
  readonly originalPdf?: FileViewerMetadata;

  constructor(message: string, recovery: "edit" | "retry", { denied = false, originalPdf }: {
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

function fileStatusError(status: number, detail?: string): FileReadFailure {
  switch (status) {
    case 0:
      return new FileReadFailure("Couldn't load the file. Check your connection and retry.", "retry");
    case 401:
    case 403:
      return new FileReadFailure("You don't have access to this file.", "edit", { denied: true });
    case 404:
      return new FileReadFailure(detail || "File not found.", "edit");
    case 409:
      return new FileReadFailure(detail || "The file path changed. Reopen the file to check its current location.", "retry");
    case 413:
      return new FileReadFailure(detail || "This file exceeds the preview limit.", "edit");
    case 415:
      return new FileReadFailure("This file format can't be previewed.", "edit");
    default:
      return detail
        ? new FileReadFailure(detail, "edit")
        : new FileReadFailure(`Couldn't read the file (HTTP ${status}).`, "retry");
  }
}

async function fileReadError(response: Response): Promise<FileReadFailure> {
  if (response.status === 401 || response.status === 403) return fileStatusError(response.status);
  const body = await readFileErrorBody(response);
  return fileStatusError(response.status, body?.detail);
}

async function readFileMetadata(request: FileViewerRequest, init: RequestInit): Promise<FileViewerMetadata> {
  const { sessionId, path, source, sourceDirectory } = request;
  const response = await fetch(fileUrl(sessionId, path, { metadata: true, source, sourceDirectory }), init);
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
  return { initialPath: path, path: escapePathForEditing(path), position: filePositionSuffix(position ?? undefined), ...binding };
}

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
  const labels = openingLabels([
    ...state.reuseChecks,
    ...(tabs.find((tab) => tab.id === active)?.failures ?? []),
    ...(state.readerFailure ? [state.readerFailure] : []),
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
          const recovery = displayedRecovery(tab, tabs);
          const displayed = displayedReaderTab(recovery);
          const name = readerTabName(recovery);
          const pending = !selectableReaderTab(tab) && tab.id !== active;
          const retryLabel = tab.reload ? `Retry reloading ${name}` : `Retry opening ${name}`;
          return <div key={tab.id} role="presentation" data-file-slot={tab.id}
            className={cn("flex shrink-0 items-center rounded-md text-sm", tab.id === active && "bg-accent")}>
            {pending ? <span role="status" className="max-w-44 truncate px-2 text-muted-foreground" title={tab.openings[0].request.path}>
              {tab.slow ? `${name} is taking longer to load` : `${name} loading…`}
            </span> : <button type="button" role="tab" id={`${panelId(tab.id)}-tab`} data-file-tab={tab.id}
              aria-controls={panelId(tab.id)} aria-selected={tab.id === active} tabIndex={tab.id === active || (active === undefined && tab.id === selectableTabs[0]?.id) ? 0 : -1}
              title={displayed.meta?.absolute_path ?? (displayed.openings[0].readRequest ?? displayed.openings[0].request).path}
              className="max-w-44 truncate rounded-md px-2 py-1"
              onClick={() => actions.select(tab.request)} onKeyDown={(event) => onTabKey(event, tab)}>{name}</button>}
            {(pending || tab.reload) && tab.slow && pendingFileRetry(tab, tabs) && <button type="button" className={`${fileToolClass} !size-6`}
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
    {state.reuseChecks.map((check) => <ConfirmFileReuse key={check.action} action={check.action}
      request={check.readRequest ?? check.request}
      label={labels.get(check) ?? (state.reuseChecks.length > 1 ? check.request.path : undefined)}
      token={token} settle={actions.settleReuse} retry={actions.retryReuse} hidden={!!picker} />)}
    {state.notice && <p role="status" className="px-4 py-2 text-sm text-muted-foreground">{state.notice}</p>}
    <div className="relative flex min-h-0 flex-1 flex-col">
      {picker && <FilePicker key={picker.action} viewer={viewer} token={token} initial={picker.location} onClose={onClose}
        onCancel={() => actions.hidePicker(picker.action)} onSelect={(path, options) => actions.pick(picker.action, path, options)} />}
      {!picker && active === undefined && <DialogPrimitive.Title className="px-5 py-4 text-sm font-semibold">Open file</DialogPrimitive.Title>}
      {!picker && state.readerFailure && <div className="border-b border-destructive/30 px-5 py-2 text-sm text-destructive">
        <OpeningFailureNotice failure={state.readerFailure} label={labels.get(state.readerFailure)} token={token} retry={actions.retryReaderFailure} />
      </div>}
      {tabs.map((tab) => {
        const visible = !picker && active === tab.id;
        const displayed = displayedReaderTab(tab);
        return <section key={tab.id} role="tabpanel" id={panelId(tab.id)} data-file-panel
          aria-labelledby={selectableReaderTab(tab) || visible ? `${panelId(tab.id)}-tab` : undefined}
          aria-hidden={!visible || undefined} inert={!visible}
          className={cn("flex min-h-0 min-w-0 flex-1 flex-col", !visible && "invisible absolute inset-0 pointer-events-none")}>
          {tab.retained && <p role="status" className="shrink-0 px-5 py-1 text-sm text-muted-foreground">Reloading file…</p>}
          {(tab.retained ? [tab.retained, tab] : [tab]).map((attempt) => (
            <div key={attempt.attempt} aria-hidden={attempt !== displayed || undefined} inert={attempt !== displayed}
              className={cn("flex min-h-0 min-w-0 flex-1 flex-col",
                attempt !== displayed && "invisible absolute inset-0 pointer-events-none")}>
              <FileViewerInner tab={attempt === tab ? displayedRecovery(tab, tabs) : attempt}
                failures={attempt === displayed ? tab.failures : undefined}
                openingLabels={labels}
                retained={attempt === tab.retained}
                retryable={attempt.phase === "opening" && !!pendingFileRetry(attempt, tabs)}
                reload={attempt === tab ? tab.reload : undefined}
                panelId={`${panelId(tab.id)}-attempt-${attempt.attempt}`} active={visible && attempt === displayed}
                actions={actions} token={token} onClose={onClose}
                readerSize={readerSize} changeReaderSize={sizeKey ? changeReaderSize : undefined} />
            </div>
          ))}
        </section>;
      })}
    </div>
  </div>;
}

function OriginalPdfLinks({ sessionId, meta, token }: { sessionId: string; meta: FileViewerMetadata; token: string }) {
  const url = fileUrl(sessionId, meta.path, { source: meta.source, canonicalPathId: meta.canonical_path_id });
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
  const file = confirmedOpeningFile(opening);
  if (!file) return `${path} (target unconfirmed)`;
  return others.some((other) => confirmedOpeningFile(other)?.absolute_path === file.absolute_path)
    ? `${file.absolute_path} (${file.source})` : file.absolute_path;
}

function accessibleTargetName(target: string) {
  return target.trim().replace(/\s+/g, " ");
}

function openingLabels(openings: Opening[]) {
  const candidates = openings.map((opening) => {
    const label = openingLabel(opening, openings);
    const target = label ?? opening.request.path;
    return { opening, label, target, name: accessibleTargetName(target) };
  }).sort((a, b) => a.opening.action - b.opening.action);
  const reserved = new Set(candidates.map(({ name }) => name));
  const labels = new Map<Opening, string>();
  for (const { opening, label, target, name } of candidates) {
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
    } else if (label !== undefined) {
      labels.set(opening, label);
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
      <button type="button" className="break-all text-left underline" onClick={() => retry(failure)}>
        Retry opening {label}
      </button>
      {failure.recovery === "edit" && edit && <button type="button" className="break-all text-left underline" onClick={() => edit(failure)}>
        Edit path for {label}
      </button>}
      {failure.originalPdf && <OriginalPdfLinks sessionId={failure.request.sessionId} meta={failure.originalPdf} token={token} />}
    </div>
  </div>;
}

function ConfirmFileReuse({ action, request, label, token, settle, retry, hidden }: {
  action: number;
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
        if (!abort.signal.aborted) settle(action, { meta });
      } catch (error) {
        if (!abort.signal.aborted) settle(action, { error: readFailure(error) });
      }
    })();
    return () => {
      window.clearTimeout(timer);
      abort.abort();
    };
  }, [action, request, settle, token]);
  if (hidden) return null;
  return <div className="flex shrink-0 items-center gap-2 px-4 py-1 text-sm">
    <span role="status" className="min-w-0 truncate" title={label ?? request.path}>Confirming {label ?? request.path}…</span>
    {slow && <button type="button" className="shrink-0 underline" aria-label={label ? `Retry confirmation for ${label}` : undefined}
      onClick={() => retry(action)}>Retry confirmation</button>}
    <button type="button" className="shrink-0 underline" aria-label={label ? `Cancel confirmation for ${label}` : undefined}
      onClick={() => settle(action)}>Cancel confirmation</button>
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

function FileViewerInner({ tab, failures, openingLabels, reload, retained, retryable, panelId, active, actions: tabActions, token, onClose,
  readerSize, changeReaderSize }: {
  tab: ReaderAttempt; failures?: OpeningFailure[]; openingLabels: ReadonlyMap<Opening, string>;
  reload?: ReaderTab["reload"]; retained: boolean; retryable: boolean;
  panelId: string; active: boolean; actions: FileViewerTabActions; token: string; onClose: () => void;
  readerSize: ReaderSize; changeReaderSize?: (value: string) => void;
}) {
  const viewer = tab.request;
  const { sessionId, path, source, sourceDirectory, mode } = viewer;
  const isCurrent = useCallback(() => tabActions.owns(viewer), [tabActions, viewer]);
  const canInteract = useCallback(() => tabActions.canInteract(viewer), [tabActions, viewer]);
  const markSlow = useCallback(() => tabActions.markSlow(viewer), [tabActions, viewer]);
  const startPositionAction = useCallback((position?: FilePosition | null) => tabActions.locate(viewer, position),
    [tabActions, viewer]);
  const unsupported = mode === "unsupported";
  const readResolvesCommand = usesFileResolver(viewer);
  const confirmedMeta = tab.meta ?? null;
  const awaitingFileTarget = tab.phase === "opening" && !retryable;
  const watchSlowRead = tab.phase === "opening" && (mode !== "description" || confirmedMeta !== null);
  useEffect(() => {
    if (!watchSlowRead) return;
    const timer = window.setTimeout(markSlow, SLOW_FILE_READ_MS);
    return () => window.clearTimeout(timer);
  }, [markSlow, watchSlowRead]);
  const [confirmedOnStart] = useState(() => !!tab.meta);
  const reportFailure = useCallback((failure: FileReadFailure) => tabActions.fail(viewer, failure), [tabActions, viewer]);
  const markReady = useCallback(() => {
    const conflict = tabActions.ready(viewer);
    if (conflict) reportFailure(new FileReadFailure(conflict, "edit"));
  }, [tabActions, viewer, reportFailure]);
  const [reportedTarget, setCommandTarget] = useState<ShowMeEditTarget | null>(null);
  const [reportedDescription, setDescription] = useState(() => mode === "description" ? path : viewer.description);
  const [canSearchDescription, setCanSearchDescription] = useState(false);
  const latestOpening = tab.openings[0];
  const outcome = tab.phase === "recovery" ? latestOpening.failure : undefined;
  const clarification = outcome?.clarification ? outcome.message : undefined;
  const error = outcome?.clarification ? undefined : outcome ?? (unsupported ? UNSUPPORTED_LINK : undefined);
  const latestRequest = latestOpening.request;
  const latestRead = latestOpening.readRequest ?? latestRequest;
  const recoveryRequest = tab.phase === "recovery" && latestRead !== viewer ? latestRead : undefined;
  const shownRequest = recoveryRequest ?? viewer;
  const resolvesCommand = usesFileResolver(shownRequest);
  const commandTarget = recoveryRequest ? latestOpening.resolved?.target ?? null : reportedTarget;
  const description = recoveryRequest ? openingDescription(latestOpening) : reportedDescription;
  const requestedPosition = tab.position;
  const position = requestedPosition === undefined && !recoveryRequest
    ? commandTarget?.position ?? undefined : requestedPosition ?? undefined;
  const [pathDraft, setDraft] = useState(() => mode === "edit" ? fileEditDraft(path, position) : null);
  const draftRetired = (pathDraft?.openingRequest && pathDraft.openingRequest !== latestRequest) ||
    (pathDraft?.failure && !failures?.includes(pathDraft.failure));
  const draft = draftRetired ? null : pathDraft;
  const [copyStatus, setCopyStatus] = useState("");
  const [dropNotice, setDropNotice] = useState(false);
  const [markdownView, setMarkdownView] = useState<{ requested: ReaderTab["position"]; mode: "reading" | "source" }>();
  const [pathExpanded, setPathExpanded] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (scroller.current && active) applyReaderSize(scroller.current, readerSize);
  }, [readerSize, active]);
  const [positionResult, setPositionResult] = useState<{ position: FilePosition; note: string | null; footnoteId?: string }>();
  const positioned = useRef<FilePosition | undefined>(undefined);
  const reportPosition = useCallback<ReportFilePosition>((note, footnoteId) => {
    if (!active || !position || positioned.current === position || !canInteract()) return false;
    positioned.current = position;
    setPositionResult({ position, note, footnoteId });
    return true;
  }, [active, position, canInteract]);
  const locate = useCallback<LocateInDocument>((target, anchor) => {
    const nextPosition = target ? { ...target } : null;
    if (!startPositionAction(nextPosition)) return;
    // Each activation owns one scroll, even when its destination is the same.
    setMarkdownView(undefined);
    if (anchor) anchor.scrollIntoView?.({ block: "start" });
    else if (!target && scroller.current) scroller.current.scrollTop = 0;
  }, [startPositionAction]);
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
        let target = path;
        let resolved: Pick<ShowMeResolveResponse, "target" | "description"> | undefined;
        if (readResolvesCommand) {
          const url = new URL(`/api/sessions/${encodeURIComponent(sessionId)}/showme/resolve`, window.location.origin);
          if (source) url.searchParams.set("source", source);
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
            throw fileStatusError(res.status, failure?.detail);
          }
          const result = (await res.json()) as ShowMeResolveResponse;
          if (!current()) return;
          setCommandTarget(result.target ?? null);
          if (result.description) setDescription(result.description);
          resolved = { target: result.target, description: result.description };
          if (result.metadata) {
            tabActions.confirm(viewer, result.metadata, resolved);
            return;
          }
          if (!result.path) {
            const message = result.message || "Couldn't resolve that file reference.";
            if (result.description || mode === "description") {
              tabActions.fail(viewer, { message, clarification: true });
              return;
            }
            throw new FileReadFailure(message, "edit");
          }
          target = result.path;
        }
        const m = await readFileMetadata({ sessionId, path: target, source, sourceDirectory }, init);
        if (current()) tabActions.confirm(viewer, m, resolved);
      } catch (e) {
        if (current()) reportFailure(readFailure(e));
      }
    })();
    return () => abort.abort();
  }, [viewer, sessionId, path, source, sourceDirectory, mode, token, unsupported, readResolvesCommand, confirmedOnStart, isCurrent, reportFailure, tabActions]);

  const visibleMeta = error && (error.denied || reload) ? null : confirmedMeta;
  const locatedPosition = positionResult?.position === position ? positionResult : undefined;
  const editablePosition = locatedPosition?.footnoteId ? { kind: "heading" as const, slug: locatedPosition.footnoteId } : position;
  const positionNote = visibleMeta
    ? positionNoteFor(position, visibleMeta.kind) ?? locatedPosition?.note
    : undefined;
  const positionPending = !!position && positionNote === undefined;
  const showSource = markdownView && markdownView.requested === tab.position
    ? markdownView.mode === "source" : position?.kind === "lines";
  const displayPath = visibleMeta?.absolute_path ?? commandTarget?.path ?? (resolvesCommand && description ? "" : shownRequest.path);
  const filename = (error && reload?.name) || displayPath.split("/").pop() || displayPath ||
    (description ? "Find a file" : "Open file");
  const canonicalUrl = visibleMeta && fileUrl(sessionId, visibleMeta.path, {
    source: visibleMeta.source, canonicalPathId: visibleMeta.canonical_path_id,
  });
  const bytesUrl = canonicalUrl && `${canonicalUrl}&token=${encodeURIComponent(token)}`;
  const previewUrl = bytesUrl && (visibleMeta?.kind === "pdf" ? `${bytesUrl}&preview=true` : bytesUrl);
  const search = (text: string) => open(text, { mode: "description" });
  const recoveryRead = tab.phase === "recovery" ? retryFileRead(latestOpening) : undefined;
  const retry = () => {
    if (tab.phase === "opening") {
      tabActions.retryPending(viewer);
    } else if (tab.phase === "ready" && viewer.reloadCommand) {
      tabActions.reload(viewer, viewer.reloadCommand, {
        mode: "command", source: confirmedMeta?.source ?? source, position: requestedPosition,
      });
    } else if (recoveryRead) {
      tabActions.reload(viewer, recoveryRead.path, { ...recoveryRead, position: requestedPosition });
    } else if (confirmedMeta && !sourceDirectory && description) {
      tabActions.reload(viewer, confirmedMeta.absolute_path, { source: confirmedMeta.source, position, description });
    } else if (description && commandTarget) {
      const candidate = commandTarget.path;
      tabActions.reload(viewer, candidate.startsWith("~") ? `./${candidate}` : candidate, { description });
    } else {
      tabActions.reload(viewer, path, {
        ...viewer, source: confirmedMeta?.source ?? source, position: requestedPosition, description,
      });
    }
  };
  const choose = () => tabActions.showPicker(viewer);
  const edit = () => {
    if (!tabActions.canEditPath(viewer)) return;
    const target = recoveryRequest && !resolvesCommand ? recoveryRequest.path
      : resolvesCommand && description && !confirmedMeta && !commandTarget ? ""
      : editablePath(shownRequest, visibleMeta ?? (resolvesCommand && !commandTarget ? confirmedMeta : null), commandTarget);
    const openingRequest = tab.phase === "ready" ? undefined : latestRequest;
    setDraft(fileEditDraft(target, editablePosition, { openingRequest }));
    setDropNotice(false);
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
  if (mode !== "edit" && !unsupported && !awaitingFileTarget && (!resolvesCommand || !description || confirmedMeta || recoveryRead)) {
    actions.push({ label: "Reload file", icon: <IconRefresh size={16} />, run: retry, disabled: draft !== null });
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
      {!!failures?.length && <div className="max-h-28 shrink-0 space-y-2 overflow-y-auto border-b border-destructive/30 px-5 py-2 text-sm text-destructive">
        {failures.map((failure) => <OpeningFailureNotice key={failure.action} failure={failure} label={openingLabels.get(failure)} token={token}
          retry={(target) => tabActions.reopenFailure(viewer, target)}
          edit={retained ? undefined : (target) => {
            if (!tabActions.canEditPath(viewer)) return;
            const read = failedFileRead(target);
            setDraft(fileEditDraft(read.path, read.position, { failure: target }));
            setDropNotice(false);
          }} />)}
      </div>}
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
      {positionNote && !error && <div role="note" className="px-5 py-2 text-sm text-muted-foreground">{positionNote}</div>}
      {!retained && draft !== null && (
        <form className="flex flex-wrap items-end gap-2 border-b border-border p-3" onSubmit={(event) => {
          event.preventDefault();
          if (canSubmitDraft && tabActions.canEditPath(viewer)) {
            const target = literalDraftPath.startsWith("~") ? `./${literalDraftPath}` : literalDraftPath;
            const editedPosition = parseFilePosition(draft.position.trim()) ?? null;
            const samePath = literalDraftPath === draft.initialPath;
            const absolute = target.startsWith("/");
            // The intent can confirm a source while the draft is open. A
            // later byte denial hides metadata but cannot change that source.
            const failure = draft.failure;
            const failedRead = failure ? failedFileRead(failure) : undefined;
            const editedRequest = failure?.request ?? shownRequest;
            const currentFile = !failure && !recoveryRequest && tab.phase === "ready" ? confirmedMeta : null;
            const boundSource = (failedRead ? failedRead.source
              : confirmedMeta?.source ?? recoveryRead?.source ?? commandTarget?.source) ?? editedRequest.source;
            const selectedSource = samePath ? boundSource : absolute ? sourceContainingPath(boundSource, filePathSegments(target))
              : currentFile?.source ?? editedRequest.source;
            const optionsRequest = samePath ? failedRead ?? recoveryRead ?? editedRequest : editedRequest;
            const options = { source: selectedSource,
              sourceDirectory: absolute ? undefined : currentFile?.canonical_directory ?? optionsRequest.sourceDirectory,
              position: editedPosition, description: failure ? optionsRequest.description : description,
              reloadCommand: samePath ? optionsRequest.reloadCommand : undefined };
            if (failure) tabActions.reopenFailure(viewer, failure, { path: target, options, samePath });
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
            {dropNotice && <span role="status" className="text-xs text-muted-foreground">
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
          <FileError failure={error} compact={!!description} reload={!!reload}
            originalPdf={error.originalPdf && reload ?
              <OriginalPdfLinks sessionId={sessionId} meta={error.originalPdf} token={token} /> : undefined}
            retry={!resolvesCommand || !description || confirmedMeta || recoveryRead || commandTarget ? retry : undefined}
            edit={!retained && draft === null ? edit : undefined} choose={choose}
            search={canSearchDescription ? () => search(path) : undefined} />
        ) : mode === "edit" || clarification ? null : !visibleMeta || !canonicalUrl || !previewUrl ? (
          tab.slow ? <SlowFileLoading onRetry={awaitingFileTarget ? undefined : retry}>The file has not finished loading.</SlowFileLoading>
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
            position={position}
            onPosition={reportPosition}
            onLocate={locate}
            startPositionAction={startPositionAction}
            showSource={showSource}
            onOpen={open}
            onReady={markReady}
            onError={reportFailure}
            active={active}
            onRetry={retry}
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

function FileError({ failure, retry, edit, choose, search, compact, reload, originalPdf }: {
  failure: OpenError; retry?: () => void; edit?: () => void; choose: () => void; search?: () => void; compact: boolean; reload?: boolean;
  originalPdf?: ReactNode;
}) {
  return (
    <div className={cn("flex items-center justify-center px-6", compact ? "py-4" : "h-full")}>
      <div className="max-w-md rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
        <div className="font-semibold mb-1">Couldn't open file</div>
        <div role="alert" className="whitespace-pre-wrap break-words">{failure.message}</div>
        <div className="mt-2 flex flex-wrap gap-4">
          {(reload || failure.recovery === "retry") && retry && <button type="button" onClick={retry} className="underline">Retry</button>}
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

type LocateInDocument = (position?: FilePosition, anchor?: HTMLElement) => void;

interface TextBodyProps extends BodyProps {
  sessionId: string;
  panelId: string;
  position?: FilePosition;
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

function PdfBody(props: PdfBodyProps) {
  const { onReady } = props;
  const supported = navigator.pdfViewerEnabled !== false;
  useEffect(() => {
    if (!supported) onReady();
  }, [supported, onReady]);
  if (!supported) {
    return (
      <div className="flex h-full items-center justify-center p-6 text-sm text-muted-foreground">
        PDF preview is unavailable in this browser. Open in a new tab or download the file using the actions above.
      </div>
    );
  }
  return <PdfPreview {...props} contentUrl={`${props.contentUrl}&preview=true`} />;
}

function PdfPreview({ active, onReaderAction, onRetry, slow, ...props }: PdfBodyProps) {
  const { meta, onError, onReady, isCurrent } = props;
  const bytes = useFileContent(props, readPdf);
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
  const fail = useCallback(() => {
    if (isCurrent()) onError(new FileReadFailure("Couldn't load the preview.", "retry", { originalPdf: meta }));
  }, [isCurrent, meta, onError]);
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
  const { meta, sessionId, panelId, token, position, canInteract, onReady, onOpen, onPosition, onLocate, startPositionAction, showSource, onRetry, slow } = props;
  const text = useFileContent(props, readText);
  const [section, setSection] = useState<{ position?: FilePosition; start: number }>();
  useLayoutEffect(() => {
    // Commit the ready tab before its newly rendered links can receive input.
    if (text !== null) onReady();
  }, [text, onReady]);
  if (text === null) return slow
    ? <SlowFileLoading onRetry={onRetry}>The file has not finished loading.</SlowFileLoading> : <FileLoading />;
  if (meta.kind === "markdown" && !showSource) {
    return <MarkdownBody text={text} meta={meta} sessionId={sessionId} panelId={panelId} token={token}
      canInteract={canInteract} position={position} onPosition={onPosition} onOpen={onOpen} onLocate={onLocate} />;
  }
  const language = meta.kind === "markdown" ? "markdown" : meta.kind === "code" ? languageHint(meta.path) : null;
  return <FileSourceView raw={text} highlight={meta.kind !== "text"} language={language}
    position={position?.kind === "lines" ? position : undefined} onPosition={onPosition}
    sectionStart={section?.position === position ? section?.start : undefined}
    onSectionChange={(start) => {
      if (startPositionAction()) setSection({ position, start });
    }} />;
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

interface MarkdownBodyProps {
  text: string;
  meta: FileViewerMetadata;
  sessionId: string;
  panelId: string;
  token: string;
  canInteract: () => boolean;
  position?: FilePosition;
  onOpen: (path: string, options?: FileViewerOptions) => void;
  onPosition: ReportFilePosition;
  onLocate: LocateInDocument;
}

function MarkdownBody({ text, meta, sessionId, panelId, token, canInteract, position, onOpen, onPosition, onLocate }: MarkdownBodyProps) {
  const article = useRef<HTMLElement>(null);
  const { source, canonical_directory: directory } = meta;
  const prefix = `${panelId}-`;
  const footnoteLabel = `${prefix}footnote-label`;
  useLayoutEffect(() => {
    if (position?.kind !== "heading") return;
    const anchor = documentPositionAnchor(article.current, position.slug, prefix);
    if (!anchor) {
      onPosition("The requested heading wasn't found.");
      return;
    }
    const id = anchor.id.slice(prefix.length);
    const footnoteId = id.startsWith(HEADING_PREFIX) ? undefined : id;
    if (onPosition(null, footnoteId)) anchor.scrollIntoView?.({ block: "start" });
  }, [position, onPosition, prefix]);

  const components = useMemo<Components>(() => {
    const openReference = ({ path, position, unsupported }: FileReference) => {
      if (!canInteract()) return;
      const absolute = path.startsWith("/");
      // Preserve the full path for the server, including any symlink-sensitive
      // "..". Only a direct reference to the confirmed document can skip I/O.
      const targetSegments = filePathSegments(path);
      const targetPath = targetSegments.join("/");
      const sameFile = absolute ? `/${targetPath}` === meta.absolute_path : targetPath === meta.path.split("/").at(-1);
      if (!unsupported && sameFile) {
        onLocate(position);
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
          } else {
            onLocate(parseFilePosition(href, true));
          }
        }} />;
      },
      img: ({ node, src, alt, ...props }) => {
        void node;
        const reference = relativeImageReference(src);
        if (!reference) return <img {...props} src={src || undefined} alt={alt} />;
        const target = decodeFileReference(reference.path);
        if (!target) return <img {...props} alt={alt} />;
        const bytes = `${fileUrl(sessionId, target, { source, sourceDirectory: directory })}&token=${encodeURIComponent(token)}`;
        const fragment = reference.fragment === undefined ? "" : `#${reference.fragment}`;
        return <img {...props} src={bytes + fragment} alt={alt} />;
      },
    };
  }, [directory, meta.absolute_path, meta.path, source, sessionId, token, canInteract, footnoteLabel, onOpen, onLocate]);

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
  return <article ref={article} className="markdown md-github prose">{content}</article>;
}
