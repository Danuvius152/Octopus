import { useEffect, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { IconFile, IconFolder, IconX } from "@tabler/icons-react";
import type { FilePickerResult, FilePickerSources, FileResourceDirectory } from "../api";
import { accountFetch } from "../lib/accountFetch";
import { isCurrentFileViewer, type FileViewerIntent, type FileViewerOptions } from "../stores/sessionStore";
import type { PickerLocation } from "./fileViewerTabs";

type PickerContext = {
  viewer: FileViewerIntent;
  token: string;
  initial?: PickerLocation;
  onSelect: (path: string, options: FileViewerOptions) => void;
};

function pickerUrl(viewer: FileViewerIntent, endpoint: string, query: Record<string, string> = {}): string {
  return `${window.location.origin}/api/sessions/${encodeURIComponent(viewer.sessionId)}/files/${endpoint}?${new URLSearchParams(query)}`;
}

function usePickerRead<T>({ viewer, token }: PickerContext, url: string) {
  const [read, setRead] = useState<{ data: T } | { error: string } | null>(null);
  useEffect(() => {
    const abort = new AbortController();
    const current = () => !abort.signal.aborted && isCurrentFileViewer(viewer);
    void (async () => {
      try {
        const response = await accountFetch(url, {
          headers: { Authorization: `Bearer ${token}` }, cache: "no-store", signal: abort.signal,
        });
        if (!current()) return;
        if (!response.ok) {
          const message = response.status === 401 || response.status === 403 ? "This file source is no longer available."
            : response.status === 404 ? "This session or folder is no longer available."
              : response.status === 409 ? "The document directory changed. Choose another folder or return to the file."
              : `Couldn't list files (HTTP ${response.status}).`;
          setRead({ error: message });
          return;
        }
        const data = (await response.json()) as T;
        if (current()) setRead({ data });
      } catch {
        if (current()) setRead({ error: "Couldn't list files. Check your connection and refresh sources." });
      }
    })();
    return () => abort.abort();
  }, [viewer, token, url]);
  return read;
}

export function FilePicker({ onClose, onCancel, ...context }: PickerContext & { onClose: () => void; onCancel: () => void }) {
  const [refresh, setRefresh] = useState(0);
  return (
    <>
      <header className="flex items-center gap-3 border-b border-border px-5 py-3">
        <IconFolder size={18} className="shrink-0 text-muted-foreground" aria-hidden />
        <Dialog.Title data-file-picker-heading tabIndex={-1} className="min-w-0 flex-1 text-sm font-semibold">
          Choose a file
        </Dialog.Title>
        <button type="button" className="text-sm underline" onClick={() => setRefresh(value => value + 1)}>
          Refresh sources
        </button>
        <button type="button" className="text-sm underline" onClick={onCancel}>Cancel file picker</button>
        <button type="button" onClick={onClose} title="Close" aria-label="Close"
          className="flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground">
          <IconX size={18} />
        </button>
      </header>
      <FilePickerSourceList key={refresh} {...context} />
    </>
  );
}

function FilePickerSourceList(context: PickerContext) {
  const read = usePickerRead<FilePickerSources>(context, pickerUrl(context.viewer, "sources"));
  if (!read) return <p role="status" className="p-5 text-sm">Loading file sources…</p>;
  if ("error" in read) return <p role="alert" className="p-5 text-sm">{read.error}</p>;
  return <FilePickerBrowser {...context} sources={read.data} />;
}

function FilePickerBrowser({ sources, ...context }: PickerContext & { sources: FilePickerSources }) {
  const [location, setLocation] = useState(() => ({
    source: context.initial?.source ?? sources.cwd ?? sources.roots[0],
    directory: context.initial?.directory.path === "." ? "" : context.initial?.directory.path ?? "",
    binding: context.initial?.directory,
  }));
  const { source, directory, binding } = location;
  const sourceAvailable = source !== undefined && sources.roots.includes(source);
  const [query, setQuery] = useState("");
  const search = useRef<HTMLInputElement>(null);
  const segments = directory ? directory.split("/") : [];
  const visit = (path: string) => {
    search.current?.focus();
    setLocation({ source, directory: path, binding: undefined });
    setQuery("");
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col text-sm">
      {sources.cwd === null && <p role="note" className="px-5 pt-3 text-muted-foreground">
        The session working directory is no longer available.
      </p>}
      {source === undefined ? <p className="p-5">No authorized file sources are available.</p> : <>
        <div className="flex flex-wrap gap-3 border-b border-border px-5 py-3">
          <label className="flex min-w-0 flex-[2_1_16rem] flex-col gap-1">
            Source
            <select className="w-full rounded-md border border-border bg-background px-2 py-1 font-mono"
              value={source} title={source} onChange={event => {
                setLocation({ source: event.currentTarget.value, directory: "", binding: undefined });
              }}>
              {!sourceAvailable && <option value={source} disabled>{source}</option>}
              {sources.roots.map(root => <option key={root} value={root}>{root}</option>)}
            </select>
          </label>
          <label className="flex min-w-0 flex-[1_1_12rem] flex-col gap-1">
            Search filenames
            <input ref={search} type="search" value={query} disabled={!sourceAvailable} onChange={event => setQuery(event.currentTarget.value)}
              aria-describedby="file-picker-search-scope"
              className="w-full rounded-md border border-border bg-background px-2 py-1" />
          </label>
          <p id="file-picker-search-scope" className="w-full text-xs text-muted-foreground">
            Search covers the selected source and its subfolders. It skips symbolic links, special files, unavailable folders, and names that aren't valid UTF-8.
          </p>
        </div>
        <nav aria-label="Directory" className="flex flex-wrap gap-2 border-b border-border px-5 py-2 font-mono">
          {["Source root", ...segments].map((name, index) => {
            const path = segments.slice(0, index).join("/");
            return directory === path && !query ? <span key={path} aria-current="location" className="break-all">{name}</span>
              : <button key={path} type="button" className="break-all underline" onClick={() => visit(path)}>{name}</button>;
          })}
        </nav>
        {sourceAvailable ? <FilePickerEntries key={JSON.stringify([source, directory, query, binding?.path_id])} {...context}
          source={source} directory={directory} binding={binding} query={query} visit={visit} />
          : <p role="alert" className="px-5 py-3">This file source is no longer available.</p>}
      </>}
    </div>
  );
}

function FilePickerEntries({ source, directory, binding, query, visit, ...context }: PickerContext & {
  source: string; directory: string; binding?: FileResourceDirectory; query: string; visit: (path: string) => void;
}) {
  const selectionBinding = query ? (context.initial?.source === source ? context.initial.directory : undefined) : binding;
  const url = query ? pickerUrl(context.viewer, "find", { source, query })
    : binding ? pickerUrl(context.viewer, "browse", {
      source, source_directory: binding.path, source_directory_id: binding.path_id,
    }) : pickerUrl(context.viewer, "browse", { source, directory });
  const read = usePickerRead<FilePickerResult>(context, url);
  const select = (path: string, sourceDirectory?: FileResourceDirectory) => {
    if (!isCurrentFileViewer(context.viewer)) return;
    const target = path.startsWith("~") || !path.trim() ? `./${path}` : path;
    context.onSelect(target, { source, sourceDirectory });
  };
  if (!read) return <p role="status" className="px-5 py-3">{query ? "Searching…" : "Loading folder…"}</p>;
  if ("error" in read) return <p role="alert" className="px-5 py-3">{read.error}</p>;
  const { entries, skipped, truncated } = read.data;
  return (
    <div className="min-h-0 flex-1 overflow-auto bg-background px-5 py-3">
      {truncated && <p role="status" className="mb-2 text-muted-foreground">
        Search limit reached. Results may be incomplete. Browse folders to look beyond these results.
      </p>}
      {skipped && <p className="mb-2 text-muted-foreground">Some entries were skipped.</p>}
      {entries.length === 0 ? <p role="status">{query
        ? truncated ? "No matching files in the scanned entries." : "No matching files."
        : "No files or folders to show."}</p> : (
        <ul>
          {entries.map(entry => {
            const folder = entry.kind === "directory";
            const Icon = folder ? IconFolder : IconFile;
            const parent = entry.path.slice(0, -entry.name.length - 1) || ".";
            const sourceDirectory = selectionBinding?.path === parent ? selectionBinding : undefined;
            return <li key={entry.path}>
              <button type="button" className="flex w-full items-start gap-2 rounded-md px-2 py-2 text-left hover:bg-accent"
                onClick={() => folder ? visit(entry.path) : select(sourceDirectory ? entry.name : entry.path, sourceDirectory)}>
                <Icon size={16} className="mt-0.5 shrink-0 text-muted-foreground" aria-hidden />
                <span className="min-w-0 font-mono">
                  <span className="block whitespace-pre-wrap break-all">{entry.name}</span>
                  {query && <span className="block whitespace-pre-wrap break-all text-xs text-muted-foreground">
                    {parent}
                  </span>}
                </span>
              </button>
            </li>;
          })}
        </ul>
      )}
    </div>
  );
}
