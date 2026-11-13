import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { MessageBubble } from "./MessageBubble";
import { mockChatViewport } from "./chatViewport.test-support";
import { handleWsMessage } from "../hooks/useWebSocket";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";

const observed = vi.hoisted(() => ({ sources: [] as string[] }));
// Observe the real parser; keep its plugins, output and React subtree real.
vi.mock("react-markdown", async (importOriginal) => {
  const original = await importOriginal<typeof import("react-markdown")>();
  return { ...original, default: (props: Parameters<typeof original.default>[0]) => {
    observed.sources.push(String(props.children ?? ""));
    return original.default(props);
  } };
});

it("updates real text and attribution, preserves formatting and parses again after an actual remount", () => {
  const message = { role: "assistant" as const, type: "text", seq: 1, content: saved };
  const view = render(<MessageBubble sessionId={session.id} message={message} agentName="Original agent" />);
  const original = screen.getByRole("heading", { name: "Reasoning" });
  const before = observed.sources.length;
  view.rerender(<MessageBubble sessionId={session.id} message={{ ...message }} agentName="Renamed agent" />);
  expect(screen.getByLabelText("Renamed agent")).toBeVisible();
  expect(screen.getByRole("heading", { name: "Reasoning" })).toBe(original);
  expect(observed.sources).toHaveLength(before);

  const changed = "## Updated reasoning\n\n**Correct result** $x^2$\n\n```typescript\nconst answer = 42;\n```\n\n[Source](/guide)\n\n<script>bad()</script>";
  view.rerender(<MessageBubble sessionId={session.id} message={{ ...message, content: changed }} agentName="Renamed agent" />);
  expect(screen.queryByRole("heading", { name: "Reasoning" })).not.toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Updated reasoning" })).toBeVisible();
  expect(screen.getByText("Correct result").tagName).toBe("STRONG");
  expect(view.container.querySelector(".katex")).not.toBeNull();
  expect(view.container.querySelector("code.language-typescript")).toHaveTextContent("const answer = 42;");
  expect(screen.getByRole("link", { name: "Source" })).toHaveAttribute("href", "/guide");
  expect(view.container.querySelector("script")).toBeNull();
  expect(observed.sources).toHaveLength(before + 1);
  view.unmount();
  render(<MessageBubble sessionId={session.id} message={{ ...message, content: changed }} />);
  expect(observed.sources).toHaveLength(before + 2);
});

it("keeps streaming text literal and renders the completed content through Markdown", () => {
  const message = { role: "assistant" as const, type: "text", content: "**Still arriving**" };
  const view = render(<MessageBubble sessionId={session.id} message={message} plain />);
  expect(screen.getByText("**Still arriving**")).toBeVisible();
  expect(observed.sources).toHaveLength(0);
  view.rerender(<MessageBubble sessionId={session.id} message={message} />);
  expect(screen.getByText("Still arriving").tagName).toBe("STRONG");
  expect(observed.sources).toEqual([message.content]);
});

it("parses only actual preview/full content changes while the full-message request settles", async () => {
  useSessionStore.setState({ token: "rendering-account" });
  const preview = { role: "assistant" as const, type: "text", seq: 7, content: "## Saved preview",
    body_state: "preview" as const, detail_ref: "/api/sessions/rendering/messages/7" };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ ...preview, body_state: "inline", content: saved })));
  render(<MessageBubble sessionId={session.id} message={preview} />);
  fireEvent.click(screen.getByRole("button", { name: "View full message" }));
  await waitFor(() => expect(screen.getByRole("heading", { name: "Reasoning" })).toBeVisible());
  expect(observed.sources).toEqual([preview.content, saved]);
  fireEvent.click(screen.getByRole("button", { name: "Show preview" }));
  expect(screen.getByRole("heading", { name: "Saved preview" })).toBeVisible();
  expect(observed.sources).toEqual([preview.content, saved, preview.content]);
});

const saved = "SAVED_MARKDOWN\n\n## Reasoning\n\n中文说明与 $x^2$。\n\n| item | value |\n| --- | --- |\n| A | 42 |";
const session = { id: "rendering", name: "Reading", agent_id: "agent", status: "running", backend: "codex",
  created_at: "2026-11-13", working_dir: "/tmp", archived: false } as SessionInfo;

beforeEach(() => {
  observed.sources = [];
  sessionStorage.clear();
  mockChatViewport();
  useSessionStore.getState().setToken("");
  useSessionStore.setState({ activeSessionId: session.id, mainView: "chat", sessions: [session], archivedSessions: [],
    agents: [], credentials: [], backendModels: {},
    messages: { [session.id]: [{ role: "assistant", type: "text", seq: 1, content: saved }] },
    streamingText: {}, streamingItems: {}, pendingQuestions: {}, subagents: {}, dispatchStates: {}, dispatchUnavailable: {},
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it("keeps unchanged completed Markdown isolated from current output, background output and draft edits", async () => {
  render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={vi.fn()} answerQuestion={vi.fn()}
      connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider>);
  await waitFor(() => expect(screen.getByRole("heading", { name: "Reasoning" })).toBeVisible());
  const original = screen.getByRole("heading", { name: "Reasoning" });
  const count = () => observed.sources.filter((source) => source === saved).length;
  const before = count();
  expect(before).toBeGreaterThan(0);

  act(() => handleWsMessage({ type: "assistant_delta", session_id: session.id, content: "Live **unfinished" }));
  expect(screen.getByText("Live **unfinished")).toBeVisible();
  expect.soft(count(), "current stream must not parse the saved body again").toBe(before);

  act(() => handleWsMessage({ type: "assistant_delta", session_id: "background", content: "Background output" }));
  expect.soft(count(), "background stream must not parse the saved body again").toBe(before);

  fireEvent.change(screen.getByRole("textbox"), { target: { value: "An unsent edit" } });
  expect(screen.getByRole("textbox")).toHaveValue("An unsent edit");
  expect.soft(count(), "editing must not parse the saved body again").toBe(before);
  expect(screen.getByRole("heading", { name: "Reasoning" })).toBe(original);
  expect(screen.getByRole("cell", { name: "42" })).toBeVisible();
});
