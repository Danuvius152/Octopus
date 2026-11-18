import { useCallback, useMemo, useState, useSyncExternalStore, type ClipboardEventHandler, type KeyboardEvent, type ReactNode, type RefObject } from "react";
import { IconArrowUp, IconPaperclip, IconPlayerStop } from "@tabler/icons-react";
import type { useComposerHistory } from "../hooks/useComposerHistory";
import { caretWantsHistory } from "../lib/composerHistory";
import { isImeComposing } from "../lib/composerKeys";
import type { TextDraft } from "../lib/textDrafts";
import { useSessionStore } from "../stores/sessionStore";
import { filterSlashCommands, SlashCommandMenu, type SlashCommand } from "./SlashCommandMenu";
import { Button } from "./ui/button";

export interface ComposerEditor {
  draft: TextDraft;
  textarea: RefObject<HTMLTextAreaElement | null>;
  history: ReturnType<typeof useComposerHistory>;
  edit: (text: string) => void;
  send: () => void;
  stop: () => void;
  attach: () => void;
  paste: ClipboardEventHandler<HTMLTextAreaElement>;
  canAttach: boolean;
  uploadPending: boolean;
  hasAttachments: boolean;
}

const noDraftSubscription = () => () => {};

// Read the actual device capabilities when a hardware keyboard sends Enter.
function isTouchDevice() { return typeof window !== "undefined" && (window.matchMedia?.("(hover: none)").matches ?? false); }

export function ChatComposer({ sessionId, editor, contextMeter, running, canSteer, children }: {
  sessionId: string;
  editor?: ComposerEditor;
  contextMeter: ReactNode;
  running: boolean;
  canSteer: boolean;
  children?: ReactNode;
}) {
  const dispatchPaused = useSessionStore((state) => !!state.dispatchStates[sessionId]?.paused);
  const draft = editor?.draft;
  const readText = useCallback(() => draft?.store.getState().text ?? "", [draft]);
  const input = useSyncExternalStore(draft?.store.subscribe ?? noDraftSubscription, readText);
  const [slashIndex, setSlashIndex] = useState(0);
  const [slashDismissed, setSlashDismissed] = useState(false);
  const slashCommands = useMemo(() => filterSlashCommands(input), [input]);
  const showSlashMenu = slashCommands.length > 0 && !slashDismissed;

  const edit = (text: string) => {
    editor?.edit(text);
    editor?.history.reset();
    setSlashIndex(0);
    setSlashDismissed(false);
  };
  const completeSlash = (command: SlashCommand) => {
    editor?.edit(`${command.name} `);
    setSlashIndex(0);
    setSlashDismissed(false);
    editor?.textarea.current?.focus();
  };
  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (!editor || isImeComposing(event.nativeEvent)) return;
    if (showSlashMenu) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        const step = event.key === "ArrowDown" ? 1 : -1;
        event.preventDefault();
        setSlashIndex((index) => (index + step + slashCommands.length) % slashCommands.length);
        return;
      }
      if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        completeSlash(slashCommands[Math.min(slashIndex, slashCommands.length - 1)]);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        setSlashDismissed(true);
        return;
      }
    }
    if ((event.key === "ArrowUp" || event.key === "ArrowDown") && !event.shiftKey && !event.metaKey && !event.ctrlKey && !event.altKey) {
      const { selectionStart, selectionEnd, value } = event.currentTarget;
      const direction = event.key === "ArrowUp" ? "up" : "down";
      if (selectionStart === selectionEnd && caretWantsHistory(value, selectionStart, direction) && editor.history.recall(direction, value)) {
        event.preventDefault();
        return;
      }
    }
    // Touch keyboards use Enter for a new line and the visible send button.
    if (event.key === "Enter" && !event.shiftKey && !isTouchDevice()) {
      event.preventDefault();
      editor.send();
    }
  };
  const trimmed = input.trim();
  return (
    <div className="chat-input-bar mt-auto shrink-0 bg-background px-6 pb-5 pt-2">
      {dispatchPaused && <p className="dispatch-pause-hint mb-2 text-xs text-muted-foreground">
        Your next message takes priority. Pending results stay paused until you resume processing.
      </p>}
      <div className="relative">
        {showSlashMenu && <SlashCommandMenu commands={slashCommands} activeIndex={slashIndex}
          onSelect={completeSlash} onHoverIndex={setSlashIndex} className="absolute inset-x-0 bottom-full mb-2" />}
        <div className="zero-composer overflow-hidden rounded-[14px] border border-gray-400 bg-card transition-colors focus-within:border-primary focus-within:ring-[3px] focus-within:ring-primary/10">
          {children}
          {editor?.history.waiting && <p role="status" className="px-3 text-xs text-muted-foreground">Loading full history message…</p>}
          {editor?.history.error && <p role="alert" className="px-3 text-xs text-destructive">{editor.history.error}</p>}
          <textarea ref={editor?.textarea} value={input} readOnly={!editor} onChange={(event) => edit(event.target.value)}
            onKeyDown={handleKeyDown} onPaste={editor?.paste}
            placeholder={running ? "Send to queue, or press Esc to interrupt…" : "Send a message…"}
            rows={1} enterKeyHint={isTouchDevice() ? "enter" : "send"}
            className="w-full resize-none field-sizing-content bg-transparent px-3 pt-2 pb-0 text-sm text-foreground placeholder:text-muted-foreground/50 border-0 outline-none focus:outline-none focus:ring-0 max-h-48" />
          <div className="composer-actions flex items-center justify-between gap-2 px-1.5 pb-1.5">
            <div className="flex items-center gap-1 text-muted-foreground">
              <button type="button"
                className="btn btn-attach inline-flex items-center justify-center size-8 rounded-lg transition-colors duration-200 hover:bg-accent hover:text-foreground disabled:opacity-40 disabled:cursor-not-allowed"
                aria-label="Attach file" title="Attach file" disabled={!editor?.canAttach} onClick={editor?.attach}>
                <IconPaperclip size={16} stroke={1.5} />
              </button>
              {contextMeter}
            </div>
            <div className="flex items-center gap-1">
              {running && <Button type="button" variant="destructive" size="sm"
                className="btn btn-stop rounded-lg h-8 w-8 p-0 shrink-0" onClick={editor?.stop} aria-label="Stop" title="Stop (Esc)">
                <IconPlayerStop size={14} />
              </Button>}
              <Button type="button" size="sm" className="btn btn-send size-9 shrink-0 rounded-full p-0" onClick={editor?.send}
                disabled={!editor || editor.history.waiting
                  || (editor.uploadPending && !/^\/compact(?:\s|$)/.test(trimmed))
                  || (!trimmed && !editor.hasAttachments)}
                aria-label={running ? canSteer ? "Send to running turn" : "Queue message" : "Send message"}
                title={running ? canSteer ? "Send into the running turn — the agent gets it at its next step" : "Queue (current turn is running)" : "Send"}>
                <IconArrowUp size={16} />
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
