import { useId, type ComponentProps } from "react";
import { IconFileText } from "@tabler/icons-react";
import { defaultUrlTransform, type ExtraProps, type UrlTransform } from "react-markdown";
import type { Element } from "hast";
import type { FilePosition } from "../api";
import { decodeFileReference, isExplicitFilePath, parseFilePosition, splitFileLink, splitFileLinkPosition } from "../lib/filePosition";
import { useSessionStore } from "../stores/sessionStore";

interface FileReference {
  path: string;
  position?: FilePosition;
  unsupported?: true;
}

const localFileUrl = /^file:\/\/(?:localhost)?(?=\/[^/])/i;
const drivePath = /^[a-z]:/i;
const urlScheme = /^[a-z][a-z\d+.-]*:/i;
const siteDirectory = /^\/(?:api|apps|assets|fonts|mcp|ws)(?:\/|$)/;
const siteFiles = new Set([
  "/", "/health", "/ready", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json",
  "/index.html", "/favicon.svg", "/manifest.webmanifest", "/apple-touch-icon.png",
  "/icon-192.png", "/icon-512.png", "/icon-maskable-512.png",
]);
function reservedSitePath(path: string) {
  return siteDirectory.test(path) || siteFiles.has(path.replace(/\/+$/, "") || "/");
}

function fileReference(href: string): FileReference | undefined {
  if (!href || href.startsWith("#") || href.startsWith("?") || href.startsWith("//")) return;
  const unsupported = (): FileReference => ({ path: decodeFileReference(href) ?? href, unsupported: true });
  const local = localFileUrl.exec(href);
  if ((!local && /^file:/i.test(href)) || drivePath.test(href) || /^(?:\\|%5c)/i.test(href)) return unsupported();

  const target = local ? href.slice(local[0].length) : href;
  const link = splitFileLink(target);
  const { name: rawName, suffix } = splitFileLinkPosition(link.path, link.fragment);
  // Keep web and unknown URI schemes on the normal Markdown URL path. A
  // historical bare filename followed by :12 loses its colon before this test.
  if (!local && urlScheme.test(rawName)) return;
  const path = decodeFileReference(rawName);
  if (!path) return;
  if (local && (path.startsWith("//") || drivePath.test(path.slice(1)) || path.startsWith("/\\"))) return unsupported();
  if (!local) {
    if (reservedSitePath(path)) return;
    // Chat is served at the site root. Match its relative navigation while
    // keeping the original path for a file read.
    const normalized = new URL(rawName, "https://octopus.invalid/").pathname;
    if (normalized !== rawName && reservedSitePath(decodeFileReference(normalized) ?? normalized)) return;
    const positionedAbsolute = path.startsWith("/") && /^(?::|#L-?\d)/.test(suffix);
    if (!positionedAbsolute && !isExplicitFilePath(path)) return;
  }
  return { path, position: parseFilePosition(suffix, true) };
}

const fileReferences = new WeakMap<Element, FileReference>();

export const chatLinkUrl: UrlTransform = (href, key, node) => {
  if (key === "href" && node.tagName === "a") {
    const reference = fileReference(href);
    if (reference) {
      fileReferences.set(node, reference);
      return;
    }
  }
  return defaultUrlTransform(href);
};

type AnchorProps = ComponentProps<"a"> & ExtraProps;

export function ChatLink({ node, sessionId, ...props }: AnchorProps & { sessionId: string }) {
  const reference = node && fileReferences.get(node);
  if (!reference) return <a {...props} />;
  return <ChatFileButton reference={reference} sessionId={sessionId} title={props.title}>
    {props.children}
  </ChatFileButton>;
}

function ChatFileButton({ reference, sessionId, title, children }: Pick<AnchorProps, "title" | "children"> & {
  reference: FileReference;
  sessionId: string;
}) {
  const id = useId();
  const { path, position, unsupported } = reference;
  const location = position?.kind === "lines" ? position.end == null
    ? `line ${position.start}` : `lines ${position.start}–${position.end}`
    : position?.kind === "heading" ? `heading ${position.slug}` : undefined;
  return <button type="button" className="chat-file-link" title={title} aria-haspopup="dialog"
    aria-labelledby={`${id}-action ${id}-label${location ? ` ${id}-position` : ""}`}
    onClick={(event) => {
      event.currentTarget.focus({ preventScroll: true });
      useSessionStore.getState().openViewer(sessionId, path, { position, mode: unsupported ? "unsupported" : undefined });
    }}>
    <IconFileText size={14} aria-hidden="true" />
    <span id={`${id}-action`} hidden aria-label="Open file:" />
    <span id={`${id}-label`}>{children}</span>
    {location && <span id={`${id}-position`} aria-label={`at ${location}`} data-file-position={location} />}
  </button>;
}
