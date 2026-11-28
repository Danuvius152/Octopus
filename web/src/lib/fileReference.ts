import { defaultUrlTransform, type UrlTransform } from "react-markdown";
import type { Element } from "hast";
import type { FilePosition } from "../api";
import { decodeFileReference, isExplicitFilePath, parseFilePosition, splitFileLink, splitFileLinkPosition } from "./filePosition";

export interface FileReference {
  path: string;
  position?: FilePosition;
  unsupported?: true;
}

const localFileUrl = /^file:(?:\/\/(?:localhost)?)?(?=\/[^/])/i;
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

function fileReference(href: string, context: "chat" | "document"): FileReference | undefined {
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
    if (context === "chat" || path.startsWith("/")) {
      // The browser normalizes site routes. Document-relative file paths keep
      // their original segments for the file API.
      const normalized = new URL(rawName, "https://octopus.invalid/").pathname;
      if (normalized !== rawName && reservedSitePath(decodeFileReference(normalized) ?? normalized)) return;
    }
    const positionedAbsolute = path.startsWith("/") && /^(?::|#L-?\d)/.test(suffix);
    const documentRelative = context === "document" && !path.startsWith("/") && !urlScheme.test(target);
    if (!documentRelative && !positionedAbsolute && !isExplicitFilePath(path)) return;
  }
  return { path, position: parseFilePosition(suffix, true) };
}

const fileReferences = new WeakMap<Element, FileReference>();

function fileLinkUrl(context: "chat" | "document"): UrlTransform {
  return (href, key, node) => {
    if (key === "href" && node.tagName === "a") {
      const reference = fileReference(href, context);
      if (reference) {
        fileReferences.set(node, reference);
        return;
      }
    }
    return defaultUrlTransform(href);
  };
}

export const chatLinkUrl = fileLinkUrl("chat");
export const documentLinkUrl = fileLinkUrl("document");
export function readFileReference(node: Element | undefined) {
  return node && fileReferences.get(node);
}
