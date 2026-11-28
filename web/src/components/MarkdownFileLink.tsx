import { useId, type ComponentProps } from "react";
import { IconFileText } from "@tabler/icons-react";
import type { ExtraProps } from "react-markdown";
import type { FilePosition } from "../api";
import { readFileReference } from "../lib/fileReference";
import { useSessionStore } from "../stores/sessionStore";

type AnchorProps = ComponentProps<"a"> & ExtraProps;

export function ChatLink({ node, sessionId, ...props }: AnchorProps & { sessionId: string }) {
  const reference = readFileReference(node);
  if (!reference) return <a {...props} />;
  return <FileReferenceButton opensDialog position={reference.position} title={props.title} onOpen={() => {
    const { path, position, unsupported } = reference;
    useSessionStore.getState().openViewer(sessionId, path, { position, mode: unsupported ? "unsupported" : undefined });
  }}>
    {props.children}
  </FileReferenceButton>;
}

export function FileReferenceButton({ position, onOpen, opensDialog, title, children }: Pick<AnchorProps, "title" | "children"> & {
  position?: FilePosition;
  onOpen: () => void;
  opensDialog?: boolean;
}) {
  const id = useId();
  const location = position?.kind === "lines" ? position.end == null
    ? `line ${position.start}` : `lines ${position.start}–${position.end}`
    : position?.kind === "heading" ? `heading ${position.slug}` : undefined;
  return <button type="button" className="markdown-file-link" title={title} aria-haspopup={opensDialog ? "dialog" : undefined}
    aria-labelledby={`${id}-action ${id}-label${location ? ` ${id}-position` : ""}`}
    onClick={(event) => {
      event.currentTarget.focus({ preventScroll: true });
      onOpen();
    }}>
    <IconFileText size={14} aria-hidden="true" />
    <span id={`${id}-action`} hidden aria-label="Open file:" />
    <span id={`${id}-label`}>{children}</span>
    {location && <span id={`${id}-position`} aria-label={`at ${location}`} data-file-position={location} />}
  </button>;
}
