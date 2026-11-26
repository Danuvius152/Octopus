import type { FilePosition } from "../api";

const bareFileNames = new Set([
  "dockerfile", "makefile", "readme", "license", "notice", "changelog", "authors",
  ".gitignore", ".dockerignore", ".editorconfig", ".prettierrc", ".eslintrc",
]);

export function isExplicitFilePath(path: string): boolean {
  return path.startsWith("./") || path.startsWith("../") || path.indexOf("/", 1) !== -1 ||
    /\.[a-z][a-z\d_-]*$/i.test(path) || bareFileNames.has(path.replace(/^\//, "").toLowerCase());
}

function trailingColonLine(value: string) {
  return /:(-?[0-9]+)$/.exec(value);
}

export function splitFileLink(href: string) {
  const hash = href.indexOf("#");
  const beforeFragment = hash === -1 ? href : href.slice(0, hash);
  return {
    path: beforeFragment.split("?", 1)[0],
    fragment: hash === -1 ? undefined : href.slice(hash + 1),
  };
}

export function splitFileLinkPosition(path: string, fragment: string | undefined) {
  if (fragment !== undefined) return { name: path, suffix: `#${fragment}` };
  const colon = trailingColonLine(path);
  return { name: colon ? path.slice(0, colon.index) : path, suffix: colon?.[0] ?? "" };
}

export function decodeFileReference(value: string): string | undefined {
  try {
    return decodeURIComponent(value);
  } catch {
    return undefined;
  }
}

export function parseFilePosition(suffix: string, uri = false): FilePosition | undefined {
  if (!suffix) return undefined;
  const colon = trailingColonLine(suffix);
  const lines = colon?.index === 0 ? colon : /^#L(-?[0-9]+)(?:-L(-?[0-9]+))?$/.exec(suffix);
  if (lines) {
    const start = Number(lines[1]);
    const end = lines[2] === undefined ? start : Number(lines[2]);
    if (Number.isSafeInteger(start) && Number.isSafeInteger(end) && start > 0 && end >= start) {
      return lines[2] === undefined ? { kind: "lines", start } : { kind: "lines", start, end };
    }
  } else if (suffix.startsWith("#")) {
    const fragment = suffix.slice(1);
    const slug = uri ? decodeFileReference(fragment) : fragment;
    if (slug) return { kind: "heading", slug };
  }
  return { kind: "invalid", suffix };
}

export function filePositionSuffix(position: FilePosition | undefined): string {
  if (!position) return "";
  switch (position.kind) {
    case "lines": return position.end == null ? `:${position.start}` : `#L${position.start}-L${position.end}`;
    case "heading": return `#${position.slug}`;
    case "invalid": return position.suffix;
  }
}
