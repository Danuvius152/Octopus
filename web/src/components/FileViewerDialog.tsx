import { accountFetch as fetch } from "../lib/accountFetch";
/**
 * Modal lightbox that renders a file from a session's working_dir.
 *
 * Mounted once at the App level; opens when sessionStore.viewer is
 * non-null. Triggered either by the `/showme` resolver returning a
 * concrete path or by the model deciding on its own that showing a
 * file would be clearer than quoting it.
 *
 * Renderer dispatch (kind from /files/meta):
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

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ReactNode, HTMLAttributes } from "react";
import {
  IconCheck,
  IconCopy,
  IconDownload,
  IconExternalLink,
  IconFile,
  IconRefresh,
  IconX,
  IconZoomIn,
  IconZoomOut,
} from "@tabler/icons-react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import rehypeSlug from "rehype-slug";
import * as DialogPrimitive from "@radix-ui/react-dialog";

import "highlight.js/styles/github.css";
import "katex/dist/katex.min.css";
import "./FileViewerDialog.css";

import { useSessionStore, type FileResourceDirectory, type FileViewerIntent } from "../stores/sessionStore";
import { cn } from "../lib/utils";

type FileKind = "markdown" | "code" | "text" | "image" | "pdf";

interface FileMeta {
  path: string;
  canonical_path_id: string;
  canonical_directory: FileResourceDirectory;
  kind: FileKind;
  mime_type: string;
  size: number;
}

function fileUrl(
  sessionId: string,
  path: string,
  { metadata = false, sourceDirectory }: { metadata?: boolean; sourceDirectory?: FileResourceDirectory } = {},
): string {
  const endpoint = metadata ? "files/meta" : "files";
  const query = new URLSearchParams({ path });
  if (sourceDirectory) {
    query.set("source_directory", sourceDirectory.path);
    query.set("source_directory_id", sourceDirectory.path_id);
  }
  return `${window.location.origin}/api/sessions/${encodeURIComponent(sessionId)}/${endpoint}?${query}`;
}

async function fileReadError(response: Response): Promise<Error> {
  const body = await response.text().catch(() => "");
  try {
    const detail: unknown = JSON.parse(body).detail;
    if (typeof detail === "string" && detail) return new Error(detail);
  } catch {
    // Use the raw body when the server did not return a JSON error.
  }
  return new Error(body || `HTTP ${response.status}`);
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

export function FileViewerDialog() {
  const viewer = useSessionStore((s) => s.viewer);
  const closeViewer = useSessionStore((s) => s.closeViewer);
  const token = useSessionStore((s) => s.token);

  const open = viewer !== null;
  const onOpenChange = (next: boolean) => {
    if (!next) closeViewer();
  };

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay
          className="fixed inset-0 z-[100] bg-black/70 backdrop-blur-sm data-[state=open]:animate-in data-[state=open]:fade-in-0"
        />
        <DialogPrimitive.Content
          aria-describedby={undefined}
          className={cn(
            "fixed left-[50%] top-[50%] z-[100] flex flex-col",
            "translate-x-[-50%] translate-y-[-50%]",
            "w-[min(1100px,95vw)] h-[min(85vh,900px)]",
            "rounded-xl border border-border bg-card shadow-2xl",
            "focus:outline-none"
          )}
        >
          {viewer ? (
            <FileViewerInner
              key={viewer.id}
              viewer={viewer}
              token={token}
              onClose={closeViewer}
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

function FileViewerInner({ viewer, token, onClose }: InnerProps) {
  const { sessionId, path, sourceDirectory } = viewer;
  const [meta, setMeta] = useState<FileMeta | null>(null);
  const [error, setError] = useState<string | null>(null);

  // 1) Fetch metadata first. The frontend dispatches its renderer
  //    off `kind`, and we want a clear error before we'd otherwise
  //    show a "loading" spinner that never resolves.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(fileUrl(sessionId, path, { metadata: true, sourceDirectory }), {
          headers: { Authorization: `Bearer ${token}` },
          cache: "no-store",
        });
        if (!res.ok) throw await fileReadError(res);
        const m = (await res.json()) as FileMeta;
        if (!cancelled) setMeta(m);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId, path, sourceDirectory, token]);

  const filename = path.split("/").pop() || path;
  const bytesUrl = `${fileUrl(sessionId, path, { sourceDirectory })}&token=${encodeURIComponent(token)}`;
  const previewUrl = meta?.kind === "pdf" ? `${bytesUrl}&preview=true` : bytesUrl;

  return (
    <>
      <header className="flex items-center gap-3 border-b border-border px-5 py-3">
        <IconFile size={18} className="text-muted-foreground shrink-0" />
        <div className="min-w-0 flex-1">
          <DialogPrimitive.Title className="text-sm font-semibold text-foreground truncate">
            {filename}
          </DialogPrimitive.Title>
          {path !== filename && (
            <div className="text-xs text-muted-foreground truncate font-mono">
              {path}
            </div>
          )}
        </div>
        {meta && (
          <span className="text-xs text-muted-foreground shrink-0">
            {formatBytes(meta.size)}
          </span>
        )}
        <button
          type="button"
          onClick={() => useSessionStore.getState().openViewer(sessionId, path, { sourceDirectory })}
          className="flex items-center justify-center size-8 rounded-md text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
          title="Reload"
          aria-label="Reload file"
        >
          <IconRefresh size={16} />
        </button>
        <a
          href={previewUrl}
          target="_blank"
          rel="noreferrer"
          className="flex items-center justify-center size-8 rounded-md text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
          title="Open in new tab"
          aria-label="Open in new tab"
        >
          <IconExternalLink size={16} />
        </a>
        <a
          href={bytesUrl}
          download={filename}
          className="flex items-center justify-center size-8 rounded-md text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
          title="Download"
          aria-label="Download file"
        >
          <IconDownload size={16} />
        </a>
        <button
          type="button"
          onClick={onClose}
          className="flex items-center justify-center size-8 rounded-md text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
          title="Close"
          aria-label="Close"
        >
          <IconX size={18} />
        </button>
      </header>

      <div className="flex-1 min-h-0 overflow-auto bg-background">
        {error ? (
          <FileError message={error} />
        ) : !meta ? (
          <FileLoading />
        ) : (
          <FileBody
            meta={meta}
            bytesUrl={previewUrl}
            token={token}
            viewer={viewer}
          />
        )}
      </div>
    </>
  );
}

function FileLoading() {
  return (
    <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
      Loading…
    </div>
  );
}

function FileError({ message }: { message: string }) {
  return (
    <div className="flex h-full items-center justify-center px-6">
      <div className="max-w-md rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
        <div className="font-semibold mb-1">Couldn't open file</div>
        <div className="whitespace-pre-wrap break-words">{message}</div>
      </div>
    </div>
  );
}

interface BodyProps {
  meta: FileMeta;
  bytesUrl: string;
  token: string;
  viewer: FileViewerIntent;
}

function FileBody({ meta, bytesUrl, token, viewer }: BodyProps) {
  if (meta.kind === "image") return <ImageBody bytesUrl={bytesUrl} alt={meta.path} />;
  if (meta.kind === "pdf") return <PdfBody bytesUrl={bytesUrl} />;
  const textUrl = meta.kind === "markdown"
    ? `${fileUrl(viewer.sessionId, meta.path)}&canonical_path_id=${encodeURIComponent(meta.canonical_path_id)}`
    : bytesUrl;
  return (
    <TextBody
      meta={meta}
      bytesUrl={textUrl}
      token={token}
      viewer={viewer}
    />
  );
}

function ImageBody({ bytesUrl, alt }: { bytesUrl: string; alt: string }) {
  const [zoomed, setZoomed] = useState(false);
  return (
    <div className="flex h-full items-center justify-center p-6">
      <button
        type="button"
        onClick={() => setZoomed((z) => !z)}
        className="group relative max-h-full max-w-full focus:outline-none"
        aria-label={zoomed ? "Zoom out" : "Zoom in"}
      >
        <img
          src={bytesUrl}
          alt={alt}
          className={cn(
            "block rounded-md shadow-md transition-transform",
            zoomed ? "max-h-none max-w-none cursor-zoom-out" : "max-h-[70vh] max-w-full object-contain cursor-zoom-in"
          )}
        />
        <span className="absolute right-2 top-2 flex items-center justify-center size-7 rounded-full bg-black/50 text-white opacity-0 transition-opacity group-hover:opacity-100">
          {zoomed ? <IconZoomOut size={14} /> : <IconZoomIn size={14} />}
        </span>
      </button>
    </div>
  );
}

function PdfBody({ bytesUrl }: { bytesUrl: string }) {
  if (navigator.pdfViewerEnabled === false) {
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
    <iframe
      src={bytesUrl}
      title="PDF preview"
      className="block h-full w-full border-0 bg-white"
    />
  );
}

function TextBody({ meta, bytesUrl, token, viewer }: BodyProps) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(bytesUrl, {
          headers: { Authorization: `Bearer ${token}` },
          cache: meta.kind === "markdown" ? "no-store" : undefined,
        });
        if (!res.ok) throw await fileReadError(res);
        const body = await res.text();
        if (!cancelled) setText(body);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [bytesUrl, meta.kind, token]);

  if (error) return <FileError message={error} />;
  if (text === null) return <FileLoading />;
  if (meta.kind === "markdown") {
    return <MarkdownBody text={text} directory={meta.canonical_directory} viewer={viewer} token={token} />;
  }
  if (meta.kind === "code") return <CodeBody text={text} path={meta.path} />;
  return <PlainTextBody text={text} />;
}

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
      <pre className={className} {...rest}>
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

function documentReference(url: string | undefined) {
  if (!url || url.startsWith("/") || /^[a-z][a-z\d+.-]*:/i.test(url)) return;
  const hash = url.indexOf("#");
  const beforeFragment = hash === -1 ? url : url.slice(0, hash);
  return {
    path: beforeFragment.split("?", 1)[0],
    fragment: hash === -1 ? undefined : url.slice(hash + 1),
  };
}

function decodeReference(value: string): string | undefined {
  try {
    return decodeURIComponent(value);
  } catch {
    return undefined;
  }
}

function scrollToFragment(article: HTMLElement | null, fragment: string, footnote = false) {
  if (!article || !fragment) return;
  // GFM footnote ids contain literal percent escapes for non-ASCII labels.
  const anchor = footnote ? fragment : decodeReference(fragment);
  if (anchor === undefined) return;
  const id = footnote ? anchor : HEADING_PREFIX + anchor;
  const target = Array.from(article.querySelectorAll<HTMLElement>("[id]")).find((node) => node.id === id);
  target?.scrollIntoView({ block: "start" });
}

interface MarkdownBodyProps {
  text: string;
  directory: FileResourceDirectory;
  viewer: FileViewerIntent;
  token: string;
}

function MarkdownBody({ text, directory, viewer, token }: MarkdownBodyProps) {
  const article = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    if (viewer.fragment && useSessionStore.getState().viewer === viewer) {
      scrollToFragment(article.current, viewer.fragment);
    }
  }, [viewer]);

  const components = useMemo<Components>(() => {
    return {
      pre: MarkdownPreBlock,
      a: ({ node, href, ...props }) => {
        void node;
        const reference = documentReference(href);
        if (!reference) return <a {...props} href={href} />;
        return <a {...props} href={href} onClick={(event) => {
          event.preventDefault();
          const state = useSessionStore.getState();
          if (state.viewer !== viewer) return;
          if (!reference.path) {
            const anchor = event.currentTarget;
            const footnote = anchor.hasAttribute("data-footnote-ref") || anchor.hasAttribute("data-footnote-backref");
            scrollToFragment(article.current, reference.fragment ?? "", footnote);
            return;
          }
          const target = decodeReference(reference.path);
          if (target) state.openViewer(viewer.sessionId, target, { fragment: reference.fragment, sourceDirectory: directory });
        }} />;
      },
      img: ({ node, src, alt, ...props }) => {
        void node;
        const reference = documentReference(src);
        if (!reference) return <img {...props} src={src || undefined} alt={alt} />;
        const target = decodeReference(reference.path);
        if (!target) return <img {...props} alt={alt} />;
        const bytes = `${fileUrl(viewer.sessionId, target, { sourceDirectory: directory })}&token=${encodeURIComponent(token)}`;
        const fragment = reference.fragment === undefined ? "" : `#${reference.fragment}`;
        return <img {...props} src={bytes + fragment} alt={alt} />;
      },
    };
  }, [directory, viewer, token]);

  return (
    <article ref={article} className="markdown md-github prose prose-sm max-w-none px-8 py-6">
      <Markdown
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

function CodeBody({ text, path }: { text: string; path: string }) {
  const lang = languageHint(path);
  // rehype-highlight isn't used here — we feed text directly to
  // highlight.js to render a bare code file. Keep highlighting in
  // a `useEffect`-driven ref so React doesn't have to re-render
  // hundreds of <span>s on each parent state change.
  const ref = useRef<HTMLElement | null>(null);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const hljs = (await import("highlight.js")).default;
      if (cancelled || !ref.current) return;
      const result = lang
        ? hljs.highlight(text, { language: lang, ignoreIllegals: true })
        : hljs.highlightAuto(text);
      ref.current.innerHTML = result.value;
    })();
    return () => {
      cancelled = true;
    };
  }, [text, lang]);

  return (
    <pre className="hljs m-0 px-6 py-5 text-xs font-mono leading-relaxed bg-card overflow-x-auto">
      <code ref={ref} className={lang ? `language-${lang}` : undefined}>
        {text}
      </code>
    </pre>
  );
}

function PlainTextBody({ text }: { text: string }) {
  return (
    <pre className="m-0 px-6 py-5 text-xs font-mono leading-relaxed text-foreground bg-card whitespace-pre-wrap break-words overflow-x-auto">
      {text}
    </pre>
  );
}
