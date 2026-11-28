import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

import type { FilePosition } from "../api";
import { scrollSourceViewportTo, type ReportFilePosition } from "./fileReaderScroll";

// Keep source rows and scroll height bounded independently of the file limit.
const LINES_PER_SECTION = 2000;
const OUTSIDE_SOURCE = "The requested lines are outside this file.";

type LinePosition = Extract<FilePosition, { kind: "lines" }>;

function sourceLineBreaks(raw: string) {
  return raw.matchAll(/\r\n?|\n/g);
}

function hasSourceLine(raw: string, line: number) {
  if (!raw.length) return false;
  let current = 1;
  for (const match of sourceLineBreaks(raw)) {
    if (current === line) return true;
    if (match.index + match[0].length === raw.length) break;
    current++;
  }
  return current === line;
}

export function linePositionNote(raw: string, position: LinePosition) {
  return hasSourceLine(raw, position.end ?? position.start) ? undefined : OUTSIDE_SOURCE;
}

function sourceLines(raw: string) {
  const text = raw.replace(/\r\n?/g, "\n");
  const starts = raw.length ? [0] : [];
  const rawStarts = raw.length === text.length ? starts : [0];
  let removed = 0;
  for (const match of sourceLineBreaks(raw)) {
    const end = match.index + match[0].length;
    if (end === raw.length) break;
    removed += match[0].length - 1;
    starts.push(end - removed);
    if (rawStarts !== starts) rawStarts.push(end);
  }
  return { text, starts, rawStarts };
}

// First index where a sorted sequence satisfies the predicate.
function lowerBound<T>(items: T[], matches: (item: T) => boolean) {
  let low = 0;
  let high = items.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (matches(items[middle])) high = middle;
    else low = middle + 1;
  }
  return low;
}

function firstSourceSection(position: LinePosition | undefined) {
  if (!position) return 0;
  const start = position.start - 1;
  const end = position.end ?? position.start;
  const aligned = Math.floor(start / LINES_PER_SECTION) * LINES_PER_SECTION;
  if (end <= aligned + LINES_PER_SECTION) return aligned;
  // Keep short ranges together even when they cross an ordinary section boundary.
  const context = Math.min(100, Math.max(0, LINES_PER_SECTION - (end - start)));
  return Math.max(aligned, start - context);
}

interface SyntaxToken {
  start: number;
  end: number;
  classes: string[];
}

function syntaxTokens(owner: Document, text: string, highlighted: string): SyntaxToken[] {
  const template = owner.createElement("template");
  template.innerHTML = highlighted;
  if (template.content.textContent !== text) return [];

  const tokens: SyntaxToken[] = [];
  const walker = owner.createTreeWalker(template.content, NodeFilter.SHOW_TEXT);
  let offset = 0;
  while (walker.nextNode()) {
    const node = walker.currentNode as Text;
    const classes: string[] = [];
    for (let parent = node.parentElement; parent; parent = parent.parentElement) {
      if (parent.className) classes.push(parent.className);
    }
    const end = offset + node.length;
    if (classes.length) tokens.push({ start: offset, end, classes });
    offset = end;
  }
  return tokens;
}

function styledText(text: string, classes: string[], offset: number) {
  return classes.reduce<ReactNode>((child, className) => (
    <span key={offset} className={className}>{child}</span>
  ), text);
}

interface SourceProps {
  raw: string;
  highlight: boolean;
  language: string | null;
  position?: LinePosition;
  onPosition: ReportFilePosition;
  sectionStart?: number;
  onSectionChange: (start: number) => void;
}

export function FileSourceView({ raw, highlight, language, position, onPosition, sectionStart, onSectionChange }: SourceProps) {
  const { text, starts, rawStarts } = useMemo(() => sourceLines(raw), [raw]);
  const [tokens, setTokens] = useState<SyntaxToken[]>([]);
  const endLine = position?.end ?? position?.start;
  const located = position && endLine !== undefined && endLine <= starts.length ? position : undefined;
  const initial = firstSourceSection(located);
  const firstLine = sectionStart ?? initial;
  const firstSectionSize = initial % LINES_PER_SECTION || LINES_PER_SECTION;
  const afterLastLine = Math.min(firstLine === 0 ? firstSectionSize : firstLine + LINES_PER_SECTION, starts.length);
  const source = useRef<HTMLPreElement>(null);
  const target = useRef<HTMLSpanElement>(null);
  const [copyStatus, setCopyStatus] = useState("");

  useEffect(() => {
    const root = source.current;
    if (!root || !highlight || !text || typeof Worker === "undefined") return;
    // Keep the source readable and positioned even when a grammar takes time.
    const worker = new Worker(new URL("./fileSourceHighlight.worker.ts", import.meta.url), { type: "module" });
    const owner = root.ownerDocument;
    let pending: string | undefined;
    const applyHighlight = () => {
      if (pending === undefined) return;
      const selection = owner.getSelection();
      // Replacing text nodes during a selection would change what gets copied.
      if (selection && !selection.isCollapsed && selection.rangeCount && selection.getRangeAt(0).intersectsNode(root)) return;
      setTokens(syntaxTokens(owner, text, pending));
      pending = undefined;
    };
    worker.onmessage = ({ data }: MessageEvent<string>) => {
      worker.terminate();
      pending = data;
      applyHighlight();
    };
    worker.onerror = () => worker.terminate();
    owner.addEventListener("selectionchange", applyHighlight);
    worker.postMessage({ text, language });
    return () => {
      worker.terminate();
      owner.removeEventListener("selectionchange", applyHighlight);
    };
  }, [text, highlight, language]);

  useLayoutEffect(() => {
    if (!position) return;
    if (onPosition(located ? null : OUTSIDE_SOURCE)) {
      scrollSourceViewportTo(target.current);
    }
  }, [position, located, onPosition]);

  useEffect(() => {
    const root = source.current;
    if (!root) return;
    const owner = root.ownerDocument;
    const rawOffset = (displayOffset: number) => {
      if (displayOffset === text.length) return raw.length;
      const line = lowerBound(starts, (start) => start > displayOffset) - 1;
      return rawStarts[line] + displayOffset - starts[line];
    };
    const copy = (event: ClipboardEvent) => {
      const selection = owner.getSelection();
      if (!event.clipboardData || !selection || selection.isCollapsed || selection.rangeCount !== 1) return;
      const range = selection.getRangeAt(0);
      if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return;
      const prefix = owner.createRange();
      prefix.selectNodeContents(root);
      prefix.setEnd(range.startContainer, range.startOffset);
      const start = starts[firstLine] + prefix.toString().length;
      const end = start + range.toString().length;
      event.clipboardData.setData("text/plain", raw.slice(rawOffset(start), rawOffset(end)));
      event.preventDefault();
    };
    owner.addEventListener("copy", copy);
    return () => owner.removeEventListener("copy", copy);
  }, [firstLine, starts, rawStarts, raw, text]);

  const rows: ReactNode[] = [];
  let tokenIndex = lowerBound(tokens, (token) => token.end > starts[firstLine]);
  for (let index = firstLine; index < afterLastLine; index++) {
    const from = starts[index];
    const to = starts[index + 1] ?? text.length;
    const pieces: ReactNode[] = [];
    let offset = from;
    while (tokenIndex < tokens.length && tokens[tokenIndex].start < to) {
      const { start, end, classes } = tokens[tokenIndex];
      if (start > offset) pieces.push(text.slice(offset, start));
      const styledStart = Math.max(start, from);
      offset = Math.min(end, to);
      pieces.push(styledText(text.slice(styledStart, offset), classes, styledStart));
      if (end > to) break;
      tokenIndex++;
    }
    if (offset < to) pieces.push(text.slice(offset, to));
    const number = index + 1;
    const marked = located && endLine !== undefined && number >= located.start && number <= endLine;
    rows.push(<span key={index} ref={located?.start === number ? target : undefined}
      className="file-source-line" data-line={number} data-located={marked ? "true" : undefined}>{pieces}</span>);
  }

  const showSection = (start: number) => {
    onSectionChange(start);
    scrollSourceViewportTo(source.current);
  };
  const copyLocatedLines = async () => {
    if (!located || endLine === undefined) return;
    try {
      const text = raw.slice(rawStarts[located.start - 1], rawStarts[endLine] ?? raw.length);
      await navigator.clipboard.writeText(text);
      setCopyStatus("Highlighted lines copied.");
    } catch {
      setCopyStatus("Couldn't copy the highlighted lines.");
    }
  };
  return (
    <>
      {starts.length > LINES_PER_SECTION && (
        <nav aria-label="Source line sections" className="file-source-sections sticky top-0 z-10 flex flex-wrap items-center justify-center gap-3 border-b border-border bg-card px-3 py-2 text-sm">
          <button type="button" className="underline disabled:opacity-50" disabled={firstLine === 0}
            onClick={() => showSection(Math.max(0, firstLine - LINES_PER_SECTION))}>Previous lines</button>
          <span>Lines {firstLine + 1}–{afterLastLine} of {starts.length}</span>
          <button type="button" className="underline disabled:opacity-50" disabled={afterLastLine === starts.length}
            onClick={() => showSection(afterLastLine)}>Next lines</button>
          {located && endLine !== undefined && endLine - located.start + 1 > LINES_PER_SECTION && (
            <button type="button" className="underline" onClick={() => void copyLocatedLines()}>Copy highlighted lines</button>
          )}
        </nav>
      )}
      {copyStatus && <div role="status" className="px-5 py-1 text-sm text-muted-foreground">{copyStatus}</div>}
      <pre ref={source} tabIndex={0} data-file-source-rows={highlight ? undefined : ""} className="file-source m-0 py-5 text-foreground bg-card">
        {highlight ? <code data-file-source-rows className={language ? `language-${language}` : undefined}>{rows}</code> : rows}
      </pre>
    </>
  );
}
