import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { VirtuosoMockContext } from "react-virtuoso";
import { afterEach, expect, it, vi } from "vitest";
import { useSessionStore, type Message, type SessionInfo } from "../stores/sessionStore";
import { applyTranscript } from "../lib/transcript";
import { ChatView } from "./ChatView";
import { mockChatViewport } from "./chatViewport.test-support";

const rowRenders = vi.hoisted(() => vi.fn());
// This un-memoized timestamp is a child of every persisted row shell.
vi.mock("./MessageTime", async (original) => {
  const timestamps = await original<typeof import("./MessageTime")>();
  return { ...timestamps, MessageTime: (props: Parameters<typeof timestamps.MessageTime>[0]) => {
    rowRenders(props.iso);
    return <timestamps.MessageTime {...props} />;
  } };
});

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it("keeps mounted history rows out of draft edits and root text deltas", () => {
  mockChatViewport();
  const id = "render-count";
  const session = { id, name: "Render count", backend: "codex", status: "running" } as SessionInfo;
  useSessionStore.getState().setToken("");
  useSessionStore.setState({ activeSessionId: id, mainView: "chat", sessions: [session] });
  const messages: Message[] = Array.from({ length: 12 }, (_, index) => ({
    role: "assistant", type: "text", seq: index + 1, created_at: "2026-11-18T00:00:00Z",
    content: `Saved row ${index + 1}` + (index % 2 ? " with a line of text.".repeat(24) : ""),
  }));
  applyTranscript(id, { messages, oldest_loaded_seq: 1, next_message_seq: 13, has_more_messages: false });
  const chat = render(<VirtuosoMockContext.Provider value={{ viewportHeight: 720, itemHeight: 48 }}>
    <ChatView connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} sendMessage={vi.fn()}
      interrupt={vi.fn()} answerQuestion={vi.fn()} />
  </VirtuosoMockContext.Provider>);
  const input = screen.getByRole("textbox");
  fireEvent.change(input, { target: { value: "First draft" } });
  act(() => useSessionStore.getState().appendStreamingText(id, "First live text"));
  const mounted = chat.container.querySelectorAll("[data-message-key]").length;
  expect(mounted).toBe(12);

  rowRenders.mockClear();
  for (const value of ["Another draft", "Last nonempty draft"]) fireEvent.change(input, { target: { value } });
  expect(input).toHaveValue("Last nonempty draft");
  expect(screen.getByRole("button", { name: "Queue message" })).toBeEnabled();
  const draftRenders = rowRenders.mock.calls.length;

  rowRenders.mockClear();
  for (const delta of [", more", ", then more"]) act(() => useSessionStore.getState().appendStreamingText(id, delta));
  expect(screen.getByTestId("streaming-text")).toHaveTextContent("First live text, more, then more");
  expect({ mounted, draftRenders, streamRenders: rowRenders.mock.calls.length }).toEqual({
    mounted: 12, draftRenders: 0, streamRenders: 0,
  });
  rowRenders.mockClear();

  act(() => useSessionStore.getState().addMessage(id, { role: "assistant", type: "text", seq: 13, content: "Actually new saved text" }));
  expect(screen.getByText("Actually new saved text")).toBeInTheDocument();
  expect(rowRenders).toHaveBeenCalled();
});
