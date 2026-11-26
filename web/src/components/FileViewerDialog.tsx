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
 *   pdf      → browser PDF viewer in an <iframe>
 *
 * The session file API supplies metadata and bytes. Direct browser
 * resources such as images and PDFs authenticate with a query token.
 *
 * Visual reference: VM0's AttachmentLightbox
 * (vm0/turbo/apps/platform/src/views/zero-page/zero-attachment-chips.tsx)
 * — centered modal, ~1100px wide pane, dark backdrop, header w/
 * filename + actions in the top-right.
 */

import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ClipboardEvent, DragEvent, ReactNode, HTMLAttributes } from "react";
import {
  IconCheck,
  IconChevronDown,
  IconCopy,
  IconDownload,
  IconExternalLink,
  IconFile,
  IconRefresh,
  IconTerminal2,
  IconX,
  IconZoomIn,
  IconZoomOut,
} from "@tabler/icons-react";
import Markdown, { defaultUrlTransform, type Components, type UrlTransform } from "react-markdown";
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
import { isCurrentFileViewer as isCurrentViewer, useSessionStore, type FileViewerIntent, type FileViewerOptions } from "../stores/sessionStore";
import { cn } from "../lib/utils";
import { decodeFileReference, filePositionSuffix, isExplicitFilePath, parseFilePosition, splitFileLink, splitFileLinkPosition } from "../lib/filePosition";
import { FileSourceView } from "./FileSourceView";
import { FilePicker } from "./FilePicker";
import { applyReaderSize, type ReportFilePosition } from "./fileReaderScroll";

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

  constructor(message: string, recovery: "edit" | "retry", denied = false) {
    super(message);
    this.recovery = recovery;
    this.denied = denied;
  }
}

function fileStatusError(status: number, detail?: string): FileReadFailure {
  switch (status) {
    case 0:
      return new FileReadFailure("Couldn't load the file. Check your connection and retry.", "retry");
    case 401:
    case 403:
      return new FileReadFailure("You don't have access to this file.", "edit", true);
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

async function readFileErrorBody(response: Response): Promise<ShowMeFileErrorResponse | undefined> {
  const body = await response.json().catch(() => undefined);
  return typeof body?.detail === "string" ? body : undefined;
}

function readFailure(error: unknown): FileReadFailure {
  return error instanceof FileReadFailure ? error : fileStatusError(0);
}

function editablePath(viewer: FileViewerIntent, meta: FileViewerMetadata | null, target: ShowMeEditTarget | null): string {
  if (meta) return meta.absolute_path;
  if (target) return target.path;
  if (viewer.mode === "command" && viewer.path.startsWith('"')) {
    try { return JSON.parse(viewer.path) as string; }
    catch { return viewer.path; }
  }
  return viewer.path;
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

function fileEditDraft(path: string, position: FilePosition | undefined) {
  return { initialPath: path, path: escapePathForEditing(path), position: filePositionSuffix(position) };
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
  useLayoutEffect(() => {
    const content = focusReturn.current?.content;
    if (content instanceof HTMLElement) {
      if (viewer?.mode === "picker") content.querySelector<HTMLElement>("[data-file-picker-heading]")?.focus();
      if (viewer?.mode === "description") content.querySelector<HTMLElement>("[data-description-heading]")?.focus();
    }
  }, [viewer]);
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
          {viewer?.mode === "picker" ? (
            <FilePicker key={viewer.id} viewer={viewer} token={token} onClose={close} />
          ) : viewer ? (
            <FileViewerInner
              key={viewer.id}
              viewer={viewer}
              token={token}
              onClose={close}
            />
          ) : null}
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

function FileViewerInner({ viewer, token, onClose }: InnerProps) {
  const { sessionId, path, source, sourceDirectory, mode } = viewer;
  const unsupported = mode === "unsupported";
  const resolvesCommand = mode === "command" || mode === "description";
  const [confirmedMeta, setConfirmedMeta] = useState<FileViewerMetadata | null>(null);
  const [error, setError] = useState<FileReadFailure | null>(() => unsupported
    ? new FileReadFailure("This file link uses an unsupported path format. Enter a path on the server.", "edit")
    : null);
  const [commandTarget, setCommandTarget] = useState<ShowMeEditTarget | null>(null);
  const [description, setDescription] = useState(() => mode === "description" ? path : viewer.description);
  const [clarification, setClarification] = useState<string>();
  const [canSearchDescription, setCanSearchDescription] = useState(false);
  const position = commandTarget?.position ?? viewer.position;
  const [draft, setDraft] = useState(() => mode === "edit" ? fileEditDraft(path, viewer.position) : null);
  const [copyStatus, setCopyStatus] = useState("");
  const [dropNotice, setDropNotice] = useState(false);
  const [markdownView, setMarkdownView] = useState<"reading" | "source" | null>(null);
  const [pathExpanded, setPathExpanded] = useState(false);
  const identityId = useSessionStore((s) => s.identity?.user_id);
  const sizeKey = identityId === undefined ? undefined
    : identityId === null ? "octopus_file_reader_size:legacy" : `octopus_file_reader_size:account:${identityId}`;
  const savedSize = useMemo(() => readReaderSize(sizeKey), [sizeKey]);
  const [preference, setPreference] = useState<{ key: string; size: ReaderSize }>();
  const readerSize = preference && preference.key === sizeKey ? preference.size : savedSize;
  const scroller = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (scroller.current) applyReaderSize(scroller.current, readerSize);
  }, [readerSize]);
  const changeReaderSize = (value: string) => {
    if (!sizeKey || !isCurrentViewer(viewer) || !isReaderSize(value)) return;
    setPreference({ key: sizeKey, size: value });
    try { localStorage.setItem(sizeKey, value); } catch { /* Keep the in-view preference. */ }
  };
  const [textPositionNote, setTextPositionNote] = useState<string | null>();
  const positioned = useRef(false);
  const reportPosition = useCallback<ReportFilePosition>((note) => {
    if (positioned.current || !isCurrentViewer(viewer)) return false;
    positioned.current = true;
    setTextPositionNote(note);
    return true;
  }, [viewer]);

  useEffect(() => {
    if (mode === "edit" || unsupported) return;
    const abort = new AbortController();
    const current = () => !abort.signal.aborted && isCurrentViewer(viewer);
    const init = {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
      signal: abort.signal,
    } as const;
    (async () => {
      try {
        let target = path;
        if (resolvesCommand) {
          // StrictMode can retire an effect before its replacement dispatches.
          await Promise.resolve();
          if (!current()) return;
          const res = await fetch(`${window.location.origin}/api/sessions/${encodeURIComponent(sessionId)}/showme/resolve`, {
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
              throw new FileReadFailure(failure.detail, "edit", true);
            }
            throw fileStatusError(res.status, failure?.detail);
          }
          const result = (await res.json()) as ShowMeResolveResponse;
          if (!current()) return;
          if (result.description) setDescription(result.description);
          setCommandTarget(result.target ?? null);
          if (result.metadata) {
            setConfirmedMeta(result.metadata);
            return;
          }
          if (!result.path) {
            const message = result.message || "Couldn't resolve that file reference.";
            if (result.description || mode === "description") {
              setClarification(message);
              return;
            }
            throw new FileReadFailure(message, "edit");
          }
          target = result.path;
        }
        const res = await fetch(fileUrl(sessionId, target, { metadata: true, source, sourceDirectory }), init);
        if (!current()) return;
        if (!res.ok) throw await fileReadError(res);
        const m = (await res.json()) as FileViewerMetadata;
        if (current()) setConfirmedMeta(m);
      } catch (e) {
        if (current()) setError(readFailure(e));
      }
    })();
    return () => abort.abort();
  }, [viewer, sessionId, path, source, sourceDirectory, mode, token, unsupported, resolvesCommand]);

  const visibleMeta = error?.denied ? null : confirmedMeta;
  const positionNote = visibleMeta ? positionNoteFor(position, visibleMeta.kind) ?? textPositionNote : undefined;
  const positionPending = !!position && positionNote === undefined;
  const showSource = (markdownView ?? (position?.kind === "lines" ? "source" : "reading")) === "source";
  const displayPath = visibleMeta?.absolute_path ?? commandTarget?.path ?? (resolvesCommand && description ? "" : path);
  const filename = displayPath.split("/").pop() || displayPath || (description ? "Find a file" : "Open file");
  const canonicalUrl = visibleMeta && fileUrl(sessionId, visibleMeta.path, {
    source: visibleMeta.source, canonicalPathId: visibleMeta.canonical_path_id,
  });
  const bytesUrl = canonicalUrl && `${canonicalUrl}&token=${encodeURIComponent(token)}`;
  const previewUrl = bytesUrl && (visibleMeta?.kind === "pdf" ? `${bytesUrl}&preview=true` : bytesUrl);
  const open = (target: string, options?: FileViewerOptions) => {
    const state = useSessionStore.getState();
    if (isCurrentViewer(viewer)) state.openViewer(sessionId, target, options);
  };
  const search = (text: string) => open(text, { mode: "description" });
  const retry = () => {
    if (description && confirmedMeta) {
      open(confirmedMeta.absolute_path, { source: confirmedMeta.source, position, description });
    } else if (description && commandTarget) {
      const candidate = commandTarget.path;
      open(candidate.startsWith("~") ? `./${candidate}` : candidate, { description });
    } else {
      open(path, { source, sourceDirectory, position: viewer.position, mode, description });
    }
  };
  const choose = () => open("", { mode: "picker" });
  const edit = () => {
    const target = resolvesCommand && description && !visibleMeta && !commandTarget ? ""
      : editablePath(viewer, visibleMeta, commandTarget);
    setDraft(fileEditDraft(target, position));
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
    if (!visibleMeta || !isCurrentViewer(viewer)) return;
    const absolute = visibleMeta.absolute_path;
    const suffix = positionNote === null ? filePositionSuffix(position) : "";
    const text = command ? `/showme ${JSON.stringify(absolute)} --source ${JSON.stringify(visibleMeta.source)}${suffix}` : absolute;
    try {
      await navigator.clipboard.writeText(text);
      if (isCurrentViewer(viewer)) setCopyStatus(command ? "Command copied." : "Path copied.");
    } catch {
      if (isCurrentViewer(viewer)) setCopyStatus(command ? "Couldn't copy the command." : "Couldn't copy the file path.");
    }
  };

  const literalDraftPath = draft ? unescapeEditedPath(draft.path) : "";
  const canSubmitDraft = draft !== null && !!literalDraftPath.trim() && (!unsupported || literalDraftPath !== draft.initialPath);
  const actions: FileAction[] = [];
  if (draft === null && error?.recovery !== "edit" && (!resolvesCommand || confirmedMeta || error || clarification)) {
    actions.push({ label: "Edit path", icon: <IconFile size={16} />, run: edit, movesFocus: true });
  }
  if (visibleMeta) {
    actions.push(
      { label: "Copy path", icon: <IconCopy size={16} />, run: () => void copy() },
      { label: "Copy /showme command", icon: <IconTerminal2 size={16} />, run: () => void copy(true), disabled: positionPending && !error },
    );
  }
  if (mode !== "edit" && !unsupported && (!resolvesCommand || !description || confirmedMeta)) {
    actions.push({ label: "Reload file", icon: <IconRefresh size={16} />, run: retry });
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
          <DialogPrimitive.Title data-description-heading tabIndex={-1} title={filename} className="min-w-0 flex-1 truncate font-semibold text-foreground">
            {filename}
          </DialogPrimitive.Title>
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
              value={readerSize} onChange={(event) => changeReaderSize(event.currentTarget.value)} disabled={!sizeKey}>
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
            onClick={() => setMarkdownView("reading")}>Reading</button>
          <button type="button" aria-pressed={showSource} disabled={positionPending}
            className="rounded px-3 py-1 aria-pressed:bg-accent aria-pressed:font-semibold"
            onClick={() => setMarkdownView("source")}>Source</button>
        </div>
      )}
      {positionNote && !error && <div role="note" className="px-5 py-2 text-sm text-muted-foreground">{positionNote}</div>}
      {draft !== null && (
        <form className="flex flex-wrap items-end gap-2 border-b border-border p-3" onSubmit={(event) => {
          event.preventDefault();
          if (canSubmitDraft) {
            const target = literalDraftPath.startsWith("~") ? `./${literalDraftPath}` : literalDraftPath;
            const editedPosition = parseFilePosition(draft.position.trim());
            const samePath = literalDraftPath === draft.initialPath;
            const absolute = target.startsWith("/");
            // The intent can confirm a source while the draft is open. A
            // later byte denial hides metadata but cannot change that source.
            const boundSource = confirmedMeta?.source ?? commandTarget?.source ?? source;
            const selectedSource = samePath ? boundSource : absolute ? undefined : source;
            open(target, { source: selectedSource, sourceDirectory: absolute ? undefined : sourceDirectory, position: editedPosition, description });
          }
        }}>
          <div className="flex min-w-44 flex-1 flex-col gap-1 text-sm">
            <label htmlFor="file-path-draft">File path</label>
            <textarea id="file-path-draft" ref={mode === "edit" ? undefined : focusTextarea}
              rows={2} className="rounded-md border border-border bg-background px-2 py-1 font-mono"
              value={draft.path}
              aria-describedby={draft.path.includes("␛") ? "file-path-escapes" : undefined}
              onChange={(event) => {
                updateDraft("path", event.currentTarget.value);
                setDropNotice(false);
              }}
              onCopy={copyPathSelection}
              onCut={copyPathSelection}
              onPaste={pastePath}
              onDragStart={startPathDrag}
              onDrop={dropPath} />
            {draft.path.includes("␛") && <span id="file-path-escapes" className="text-xs text-muted-foreground">
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
          <FileError failure={error} compact={!!description}
            retry={!resolvesCommand || !description || confirmedMeta || commandTarget ? retry : undefined}
            edit={draft === null ? edit : undefined} choose={choose}
            search={canSearchDescription ? () => search(path) : undefined} />
        ) : mode === "edit" || clarification ? null : !visibleMeta || !canonicalUrl || !previewUrl ? (
          <FileLoading>{mode === "description" ? "Searching the conversation…"
            : mode === "command" ? "Loading file or searching the conversation…" : "Loading…"}</FileLoading>
        ) : (
          <FileBody
            meta={visibleMeta}
            contentUrl={canonicalUrl}
            previewUrl={previewUrl}
            token={token}
            viewer={viewer}
            position={position}
            onPosition={reportPosition}
            showSource={showSource}
            onError={setError}
            onRetry={retry}
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

function FileError({ failure, retry, edit, choose, search, compact }: {
  failure: FileReadFailure; retry?: () => void; edit?: () => void; choose: () => void; search?: () => void; compact: boolean;
}) {
  return (
    <div className={cn("flex items-center justify-center px-6", compact ? "py-4" : "h-full")}>
      <div className="max-w-md rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
        <div className="font-semibold mb-1">Couldn't open file</div>
        <div role="alert" className="whitespace-pre-wrap break-words">{failure.message}</div>
        <div className="mt-2 flex flex-wrap gap-4">
          {failure.recovery === "retry" ? retry && <button type="button" onClick={retry} className="underline">Retry</button>
            : edit && <button type="button" onClick={edit} className="underline">Edit path</button>}
          <button type="button" onClick={choose} className="underline">Choose a file</button>
          {search && <button type="button" onClick={search} className="underline">Search by description</button>}
        </div>
      </div>
    </div>
  );
}

interface BodyProps {
  meta: FileViewerMetadata;
  contentUrl: string;
  token: string;
  viewer: FileViewerIntent;
  onError: (error: FileReadFailure) => void;
}

interface TextBodyProps extends BodyProps {
  position?: FilePosition;
  onPosition: ReportFilePosition;
  showSource: boolean;
}

function FileBody({ previewUrl, onRetry, ...props }: TextBodyProps & { previewUrl: string; onRetry: () => void }) {
  const { meta, viewer, onError } = props;
  const mediaError = (status: number | undefined) => {
    if (!isCurrentViewer(viewer)) return;
    onError(status != null && (status === 0 || status >= 400)
      ? fileStatusError(status) : new FileReadFailure("Couldn't load the preview.", "retry"));
  };
  if (meta.kind === "image") return <ImageBody {...props} />;
  if (meta.kind === "pdf") return <PdfBody bytesUrl={previewUrl} onError={mediaError} onRetry={onRetry} />;
  return <TextBody {...props} />;
}

async function readImage(response: Response): Promise<string> {
  const image = await response.blob();
  if (image.type !== "image/svg+xml") return URL.createObjectURL(image);
  // A data URL stays opaque if opened as a document from the image menu.
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new FileReadFailure("Couldn't load the preview.", "retry"));
    reader.readAsDataURL(image);
  });
}

function releaseImage(url: string) {
  if (url.startsWith("blob:")) URL.revokeObjectURL(url);
}
const readText = (response: Response) => response.text();

function ImageBody(props: BodyProps) {
  const { meta, onError, viewer } = props;
  const imageUrl = useFileContent(props, readImage, releaseImage);
  const [zoomed, setZoomed] = useState(false);
  const [loaded, setLoaded] = useState(false);
  return (
    <div className="flex h-full items-center justify-center p-6" aria-busy={!loaded}>
      {!loaded && <FileLoading />}
      {imageUrl && (
        <button
          type="button"
          onClick={() => setZoomed((z) => !z)}
          hidden={!loaded}
          className="group relative max-h-full max-w-full focus:outline-none"
          aria-label={zoomed ? "Zoom out" : "Zoom in"}
        >
          <img
            src={imageUrl}
            alt={meta.path}
            onLoad={() => setLoaded(true)}
            onError={() => {
              if (isCurrentViewer(viewer)) {
                onError(new FileReadFailure("Couldn't load the preview.", "retry"));
              }
            }}
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

function PdfBody({ bytesUrl, onError, onRetry }: {
  bytesUrl: string; onError: (status: number | undefined) => void; onRetry: () => void;
}) {
  const [loaded, setLoaded] = useState(false);
  const [slow, setSlow] = useState(false);
  const supported = navigator.pdfViewerEnabled !== false;
  const frameUrl = `${bytesUrl}&frame=true`;
  useEffect(() => {
    if (!supported || loaded) return;
    // A frame can stay silent after disconnecting or while bytes are in flight.
    const timer = window.setTimeout(() => setSlow(true), 5_000);
    return () => window.clearTimeout(timer);
  }, [loaded, supported]);
  if (!supported) {
    return (
      <div className="flex h-full items-center justify-center p-6 text-sm text-muted-foreground">
        PDF preview is unavailable in this browser. Open in a new tab or download the file using the actions above.
      </div>
    );
  }
  // Chromium blocks its native PDF viewer in an HTML-sandboxed frame.
  // This URL selects only PDF bytes through the existing file security gate;
  // the browser's PDF engine owns document rendering.
  return (
    <div className="relative h-full" aria-busy={!loaded}>
      {!loaded && <div className="absolute inset-0 z-10 bg-background">
        {slow ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-sm text-muted-foreground">
            <p role="status">The PDF preview has not finished loading.</p>
            <button type="button" className="underline" onClick={onRetry}>Retry</button>
          </div>
        ) : <FileLoading />}
      </div>}
      <iframe
        src={frameUrl}
        title="PDF preview"
        className="block h-full w-full border-0 bg-white"
        onError={() => onError(0)}
        onLoad={(event) => {
          const response = event.currentTarget.contentDocument;
          // Firefox's PDF.js runs with its own principal, so only its load
          // event is visible. Its same-origin HTTP error pages are readable.
          if (response?.contentType === "application/pdf" ||
            (!response && navigator.userAgent.includes("Firefox/"))) {
            setLoaded(true);
          } else if (response?.contentType === "application/json") {
            // The file API includes the HTTP status in the frame error document.
            let status: number | undefined;
            try {
              const body = JSON.parse(response.body?.innerText ?? "");
              if (typeof body?.status === "number") status = body.status;
            } catch { /* Unrecognized intermediary error. */ }
            onError(status);
          } else {
            onError(response ? undefined : 0);
          }
        }}
      />
    </div>
  );
}

function useFileContent<T>(
  { contentUrl, token, viewer, onError }: BodyProps,
  read: (response: Response) => Promise<T>,
  release?: (body: T) => void,
) {
  const [body, setBody] = useState<T | null>(null);

  useEffect(() => {
    const abort = new AbortController();
    const current = () => !abort.signal.aborted && isCurrentViewer(viewer);
    let retained: T | null = null;
    (async () => {
      try {
        const res = await fetch(contentUrl, {
          headers: { Authorization: `Bearer ${token}` },
          cache: "no-store",
          signal: abort.signal,
        });
        if (!current()) return;
        if (!res.ok) throw await fileReadError(res);
        const content = await read(res);
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
  }, [contentUrl, token, viewer, onError, read, release]);
  return body;
}

const TextBody = memo(function TextBody(props: TextBodyProps) {
  const { meta, viewer, token, position, onPosition, showSource } = props;
  const text = useFileContent(props, readText);
  const [sectionStart, setSectionStart] = useState<number>();
  if (text === null) return <FileLoading />;
  if (meta.kind === "markdown" && !showSource) {
    return <MarkdownBody text={text} source={meta.source} directory={meta.canonical_directory} viewer={viewer}
      token={token} position={position} onPosition={onPosition} />;
  }
  const language = meta.kind === "markdown" ? "markdown" : meta.kind === "code" ? languageHint(meta.path) : null;
  return <FileSourceView raw={text} highlight={meta.kind !== "text"} language={language}
    position={position?.kind === "lines" ? position : undefined} onPosition={onPosition}
    sectionStart={sectionStart} onSectionChange={setSectionStart} />;
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

const documentLinkUrl: UrlTransform = (url, key, node) => {
  const safeUrl = defaultUrlTransform(url);
  if (safeUrl || key !== "href" || node.tagName !== "a") return safeUrl;
  const { path, fragment } = splitFileLink(url);
  const { name, suffix } = splitFileLinkPosition(path, fragment);
  const filename = decodeFileReference(name);
  // Scheme delimiters are checked before decoding. An encoded colon is part of the filename.
  if (!suffix.startsWith(":") || name.includes(":") || !filename || /[\\/]/.test(filename) || !isExplicitFilePath(filename)) return safeUrl;
  // A relative prefix lets Markdown and the link handler distinguish a bare positioned filename from a scheme.
  return defaultUrlTransform(`./${url}`);
};

function documentReference(url: string | undefined) {
  if (!url || url.startsWith("/") || /^[a-z][a-z\d+.-]*:/i.test(url)) return;
  return splitFileLink(url);
}

function documentAnchor(article: HTMLElement | null, id: string): HTMLElement | null {
  if (!article) return null;
  return Array.from(article.querySelectorAll<HTMLElement>("[id]")).find((node) => node.id === id) ?? null;
}

interface MarkdownBodyProps {
  text: string;
  source: string;
  directory: FileResourceDirectory;
  viewer: FileViewerIntent;
  token: string;
  position?: FilePosition;
  onPosition: ReportFilePosition;
}

function MarkdownBody({ text, source, directory, viewer, token, position, onPosition }: MarkdownBodyProps) {
  const article = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    if (position?.kind !== "heading") return;
    const anchor = documentAnchor(article.current, HEADING_PREFIX + position.slug);
    if (onPosition(anchor ? null : "The requested heading wasn't found.")) {
      anchor?.scrollIntoView?.({ block: "start" });
    }
  }, [position, onPosition]);

  const components = useMemo<Components>(() => {
    return {
      pre: MarkdownPreBlock,
      table: ({ node, children, ...props }) => {
        void node;
        return <div className="file-markdown-table" tabIndex={0} role="region" aria-label="Table">
          <table {...props}>{children}</table>
        </div>;
      },
      a: ({ node, href, ...props }) => {
        void node;
        const reference = documentReference(href);
        if (!reference) return <a {...props} href={href} />;
        return <a {...props} href={href} onClick={(event) => {
          event.preventDefault();
          const state = useSessionStore.getState();
          if (!isCurrentViewer(viewer)) return;
          if (!reference.path) {
            const anchor = event.currentTarget;
            const footnote = anchor.hasAttribute("data-footnote-ref") || anchor.hasAttribute("data-footnote-backref");
            if (!reference.fragment) return;
            // GFM footnote IDs contain literal percent escapes for non-ASCII labels.
            const slug = footnote ? reference.fragment : decodeFileReference(reference.fragment);
            if (slug) documentAnchor(article.current, footnote ? slug : HEADING_PREFIX + slug)?.scrollIntoView?.({ block: "start" });
            return;
          }
          const { name, suffix } = splitFileLinkPosition(reference.path, reference.fragment);
          const target = decodeFileReference(name);
          const filePosition = parseFilePosition(suffix, true);
          if (target) state.openViewer(viewer.sessionId, target, { position: filePosition, source, sourceDirectory: directory });
        }} />;
      },
      img: ({ node, src, alt, ...props }) => {
        void node;
        const reference = documentReference(src);
        if (!reference) return <img {...props} src={src || undefined} alt={alt} />;
        const target = decodeFileReference(reference.path);
        if (!target) return <img {...props} alt={alt} />;
        const bytes = `${fileUrl(viewer.sessionId, target, { source, sourceDirectory: directory })}&token=${encodeURIComponent(token)}`;
        const fragment = reference.fragment === undefined ? "" : `#${reference.fragment}`;
        return <img {...props} src={bytes + fragment} alt={alt} />;
      },
    };
  }, [directory, source, viewer, token]);

  return (
    <article ref={article} className="markdown md-github prose">
      <Markdown
        urlTransform={documentLinkUrl}
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[
          [rehypeSlug, { prefix: HEADING_PREFIX }],
          [rehypeHighlight, { ignoreMissing: true }],
          rehypeKatex,
        ]}
        components={components}
      >
        {text}
      </Markdown>
    </article>
  );
}
