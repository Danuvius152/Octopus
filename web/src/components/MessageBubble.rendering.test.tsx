import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { FileViewerDialog } from "./FileViewerDialog";
import { MessageBubble } from "./MessageBubble";
import { projectMarkdownOutput } from "./chatMarkdown";
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

it("opens a historical table link for the message session and preserves the selected text offsets", () => {
  const passage = "keep this passage";
  const content = [
    "| Result |", "| --- |",
    "| [**Report**](file:///tmp/Plan%20%2523%23%3A.md:12) " + passage + " |",
  ].join("\n");
  const view = render(<MessageBubble sessionId="archived-source" message={{ role: "assistant", type: "text", content }} />);
  const file = screen.getByRole("button", { name: "Open file: Report at line 12" });
  expect(screen.getByRole("cell")).toContainElement(file);
  expect(file).toHaveAttribute("aria-haspopup", "dialog");
  expect(file.textContent).toBe("Report");
  expect(file.querySelector("strong")).toHaveTextContent("Report");
  expect(useSessionStore.getState().viewer).toBeNull();

  const start = content.indexOf(passage);
  const projection = projectMarkdownOutput(content, { start, end: start + passage.length }, "selection");
  const body = view.container.querySelector(".markdown")!;
  expect(body.textContent).toBe(projection.text);
  expect(projection.spans.map(({ start, end }) => body.textContent!.slice(start, end)).join("")).toBe(passage);

  fireEvent.click(file);
  expect(useSessionStore.getState().viewer).toMatchObject({
    sessionId: "archived-source", foregroundSessionId: session.id,
    path: "/tmp/Plan %23#:.md", position: { kind: "lines", start: 12 },
  });
});

it("keeps website, fragment and unsafe URLs on the Markdown URL pipeline", () => {
  const anchors = {
    Website: "https://example.org/report.md",
    "Protocol-relative website": "//example.org/report.md",
    Endpoint: "/api/sessions",
    App: "/apps/app/assets/report.md",
    Asset: "/assets/app.js",
    Manifest: "/manifest.webmanifest",
    "Encoded endpoint": "/%61pi/sessions",
    "Normalized endpoint": "/docs/../api/sessions",
    "Normalized asset": "/./index.html",
    "Relative endpoint": "api/backends",
    "Dot-relative endpoint": "./api/backends",
    "Parent-relative endpoint": "../api/backends",
    Anchor: "#footnote",
    Unsafe: "javascript:/tmp/report.md",
  };
  const content = [
    ...Object.entries(anchors).map(([name, href]) => `[${name}](${href})`),
    "[ASCII](/tmp/result.ts:12)",
    "[Extensionless absolute](/report:12)",
    "[Relative](notes/plan.md#L2-L3)",
    "[Explicit relative](./notes/plan.md)",
    "[Bare](README:7)",
    "[Literal colon](draft%3A12.md)",
    "[Local](file://localhost/tmp/changelog.md)",
    "![Unsafe image](file:///tmp/image.png)",
  ].join("\n\n");
  render(<MessageBubble sessionId={session.id} message={{ role: "assistant", type: "text", content }} />);
  for (const [name, href] of Object.entries(anchors)) {
    const anchor = screen.getByText(name);
    expect(anchor.tagName).toBe("A");
    expect(anchor).toHaveAttribute("href", name === "Unsafe" ? "" : href);
  }
  expect(screen.getByAltText("Unsafe image")).not.toHaveAttribute("src", "file:///tmp/image.png");
  for (const [name, path, position] of [
    ["ASCII at line 12", "/tmp/result.ts", { kind: "lines", start: 12 }],
    ["Extensionless absolute at line 12", "/report", { kind: "lines", start: 12 }],
    ["Relative at lines 2–3", "notes/plan.md", { kind: "lines", start: 2, end: 3 }],
    ["Explicit relative", "./notes/plan.md", undefined],
    ["Bare at line 7", "README", { kind: "lines", start: 7 }],
    ["Literal colon", "draft:12.md", undefined],
    ["Local", "/tmp/changelog.md", undefined],
  ] as const) {
    fireEvent.click(screen.getByRole("button", { name: `Open file: ${name}` }));
    expect(useSessionStore.getState().viewer).toMatchObject({ path, position });
  }
});

it.each([
  ["Remote", "file://remote/share/report.md"],
  ["Windows drive", "C:/work/report.md"],
  ["Windows URI", "file:///C:/work/report.md"],
  ["UNC", "%5C%5Cremote%5Cshare%5Creport.md"],
])("explains an unsupported %s link without requesting a file", async (label, href) => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  render(<><MessageBubble sessionId={session.id} message={{ role: "assistant", type: "text", content: `[${label}](${href})` }} />
    <FileViewerDialog /></>);
  fireEvent.click(screen.getByRole("button", { name: `Open file: ${label}` }));
  expect(await screen.findByRole("alert")).toHaveTextContent("This file link uses an unsupported path format.");
  expect(fetch).not.toHaveBeenCalled();
});

it("keeps native Markdown structure and applies the original image entry to standalone images", () => {
  const content = [
    "- ![](/list.png)",
    "",
    "Before [![Small icon](/icon.png)](/help) after.",
    "",
    "**[*![Framed diagram](/plot.png)*](/report)**",
    "",
    "![Blocked protocol](javascript:bad)",
  ].join("\n");
  const view = render(<MessageBubble sessionId={session.id} message={{ role: "assistant", type: "text", content }} />);

  const original = screen.getByRole("link", { name: /^Open original image$/ });
  expect(screen.getByRole("listitem")).toContainElement(original);
  expect(original).toHaveAttribute("href", "/list.png");
  expect(original.querySelector("img")).toHaveAttribute("alt", "");

  const inline = screen.getByRole("link", { name: "Small icon" });
  expect(inline).toHaveAttribute("href", "/help");
  expect(screen.getByRole("img", { name: "Small icon" }).parentElement).toBe(inline);
  expect(inline.parentElement?.tagName).toBe("P");
  expect(inline.parentElement).toHaveTextContent("Before after.");

  const diagram = screen.getByRole("img", { name: "Framed diagram" });
  const authored = screen.getByRole("link", { name: "Framed diagram" });
  expect(authored).toHaveAttribute("href", "/report");
  expect(diagram.closest("em")?.closest("a")).toBe(authored);
  expect(authored.closest("strong")).not.toBeNull();
  const originalDiagram = screen.getByRole("link", { name: "Open original image: Framed diagram" });
  expect(originalDiagram).toHaveAttribute("href", "/plot.png");
  expect(authored.closest("p")).toContainElement(originalDiagram);
  expect(screen.queryByRole("link", { name: "Open original image: Blocked protocol" })).toBeNull();
  expect(screen.getByRole("img", { name: "Blocked protocol" })).not.toHaveAttribute("src", "javascript:bad");
  expect(view.container.querySelector("a a, p p, p div")).toBeNull();
});

it("preserves image order and transcript text offsets when rendering standalone image paragraphs", () => {
  const passage = "The paragraph below the images.";
  const content = [
    "[ ![Padded](/padded.png) ](/author)",
    "![Trailing space](/space.png) &#32;",
    "- ![In a tight list](/list.png)\n  - Nested item",
    passage,
    "Before ![Inline](/inline.png) after.",
    "![Nonbreaking space](/nbsp.png)&nbsp;",
    "![First of two](/first.png) ![Second of two](/second.png)",
  ].join("\n\n");
  const view = render(<MessageBubble sessionId={session.id} message={{ role: "assistant", type: "text", content }} />);
  const body = view.container.querySelector(".markdown")!;

  const padded = screen.getByRole("link", { name: "Padded" });
  expect(padded).toHaveAttribute("href", "/author");
  expect(padded.textContent).toBe("  ");
  expect(screen.getByRole("link", { name: "Open original image: Padded" })).toHaveAttribute("href", "/padded.png");
  expect(screen.getByRole("link", { name: "Open original image: Trailing space" })).toHaveAttribute("href", "/space.png");
  const inList = screen.getByRole("link", { name: "Open original image: In a tight list" });
  expect(inList.closest("li")).toContainElement(screen.getByText("Nested item"));
  expect(inList.closest("p")).toBeNull();
  expect(screen.getAllByRole("link", { name: /^Open original image:/ })).toHaveLength(3);

  expect([...body.querySelectorAll("img")].map((image) => image.alt)).toEqual([
    "Padded", "Trailing space", "In a tight list", "Inline", "Nonbreaking space", "First of two", "Second of two",
  ]);
  const start = content.indexOf(passage);
  const selection = projectMarkdownOutput(content, { start, end: start + passage.length }, "selection");
  expect(body.textContent).toBe(selection.text);
  expect(selection.spans.map(({ start, end }) => body.textContent!.slice(start, end)).join("")).toBe(passage);
});

it.each([
  [String.raw`x+\alpha`, "x"],
  [String.raw`\alpha+x+\beta`, "x"],
  ["x/y", "/"],
  [String.raw`x/y+\alpha`, "x/y"],
  ["(x+y)", "(x+y)"],
  ["α+β", "α"],
  [String.raw`\alpha=x`, "="],
] as const)("maps %s source text %s to its rendered KaTeX glyphs", (expression, literal) => {
  const prefix = "Keep $";
  const content = `${prefix}${expression}$ beside the original line`;
  const start = prefix.length + expression.indexOf(literal);
  const selection = projectMarkdownOutput(content, { start, end: start + literal.length }, "selection");
  const view = render(<MessageBubble sessionId={session.id} message={{ role: "assistant", type: "text", content }} />);
  const body = view.container.querySelector(".markdown")!;
  const visual = body.querySelector(".katex-html")!;
  const preceding = document.createRange();
  preceding.selectNodeContents(body);
  preceding.setEndBefore(visual);
  const glyphOffset = visual.textContent!.indexOf(literal);
  expect(glyphOffset).toBeGreaterThanOrEqual(0);
  expect(selection.text).toBe(body.textContent);
  const renderedStart = preceding.toString().length + glyphOffset;
  expect(selection.spans).toEqual(Array.from(literal, (_, index) => {
    const offset = renderedStart + index;
    return { start: offset, end: offset + 1 };
  }));
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
  useSessionStore.setState({ viewer: null, activeSessionId: session.id, mainView: "chat", sessions: [session], archivedSessions: [],
    agents: [], credentials: [],
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
  fireEvent.click(screen.getByRole("button", { name: "Browse loaded content" }));
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
