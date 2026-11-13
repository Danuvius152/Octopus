import { useLayoutEffect, useRef } from "react";
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MessageDetails } from "./MessageDetails";
import { useSessionStore, type Message } from "../stores/sessionStore";

const preview = (seq: number): Message => ({ role: "assistant", type: "text", seq,
  content: `Preview ${seq}`, body_state: "preview", detail_ref: `/api/sessions/s/messages/${seq}` });
const complete = (seq: number): Message => ({ role: "assistant", type: "text", seq, content: `Complete ${seq}` });

// Capture the committed public DOM before passive effects can clear a previous
// resource's state. Post-act queries alone would miss a first-render leak.
function CommitText({ content, commits, beforeEffects }: {
  content: string; commits: string[]; beforeEffects?: (element: HTMLElement) => void;
}) {
  const element = useRef<HTMLParagraphElement>(null);
  useLayoutEffect(() => {
    const root = element.current!.parentElement!;
    commits.push(root.textContent ?? "");
    beforeEffects?.(root);
  });
  return <p ref={element}>{content}</p>;
}
function Subject({ message, commits, beforeEffects, sessionId = "s" }: {
  message: Message; commits: string[]; beforeEffects?: (element: HTMLElement) => void; sessionId?: string;
}) {
  return <MessageDetails sessionId={sessionId} message={message}>{(shown) =>
    <CommitText content={String(shown.content)} commits={commits} beforeEffects={beforeEffects} />}</MessageDetails>;
}

beforeEach(() => useSessionStore.setState({ token: "detail-reader", accountGeneration: 1,
  messages: {}, messageWindows: { s: Symbol("original window") } }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it.each(["success", "failure", "loading"])("hides all of the old resource's %s state on the first new render", async (state) => {
  let finish!: (response: Response) => void;
  const fetch = vi.fn().mockImplementation(() => new Promise<Response>((resolve) => { finish = resolve; }));
  vi.stubGlobal("fetch", fetch);
  const commits: string[] = [];
  const view = render(<Subject message={preview(1)} commits={commits} />);
  fireEvent.click(view.getByRole("button", { name: "View full message" }));
  expect(view.getByRole("status")).toHaveTextContent("Loading full message");
  if (state !== "loading") await act(async () => {
    finish(state === "success" ? Response.json(complete(1)) : new Response("Unavailable", { status: 503 }));
  });
  if (state === "success") expect(view.getByText("Complete 1")).toBeVisible();
  if (state === "failure") expect(view.getByRole("alert")).toHaveTextContent("503");
  commits.length = 0;
  view.rerender(<Subject message={preview(2)} commits={commits} />);
  expect(commits[0]).toContain("Preview 2");
  expect(commits[0]).not.toMatch(/Complete 1|503|Loading full message/);
  expect(view.getByRole("button", { name: "View full message" })).toBeEnabled();
  expect(fetch).toHaveBeenCalledTimes(1);
  if (state === "loading") await act(async () => { finish(Response.json(complete(1))); });
  expect(view.queryByText("Complete 1")).not.toBeInTheDocument();
});

it.each(["success", "failure"])("keeps the new read through old cleanup and a late old %s", async (outcome) => {
  const responses: ((response: Response) => void)[] = [];
  const fetch = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => new Promise<Response>((resolve) => { responses.push(resolve); }));
  vi.stubGlobal("fetch", fetch);
  const commits: string[] = [];
  const view = render(<Subject message={preview(1)} commits={commits} />);
  fireEvent.click(view.getByRole("button", { name: "View full message" }));
  let opened = false;
  view.rerender(<Subject message={preview(2)} commits={commits} beforeEffects={(element) => {
    if (opened) return;
    opened = true;
    // Drive the rendered public control before predecessor passive cleanup.
    element.querySelector<HTMLButtonElement>("button")!.click();
  }} />);
  expect(responses).toHaveLength(2);
  const signal = fetch.mock.calls[1][1]!.signal!;
  expect(signal.aborted, "A's cleanup cannot abort B's already started read").toBe(false);
  await act(async () => { responses[0](outcome === "success" ? Response.json(complete(1)) : new Response("Old failure", { status: 503 })); });
  expect(view.getByRole("status")).toHaveTextContent("Loading full message");
  expect(view.queryByRole("alert")).not.toBeInTheDocument();
  expect(view.queryByText("Complete 1")).not.toBeInTheDocument();
  await act(async () => { responses[1](Response.json(complete(2))); });
  expect(view.getByText("Complete 2")).toBeVisible();
  expect(view.queryByRole("status")).not.toBeInTheDocument();
});

it("revokes an old read synchronously when its store window changes before React commit", async () => {
  let finish!: (response: Response) => void;
  const fetch = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => new Promise<Response>((resolve) => { finish = resolve; }));
  vi.stubGlobal("fetch", fetch);
  const commits: string[] = [];
  const view = render(<Subject message={preview(1)} commits={commits} />);
  fireEvent.click(view.getByRole("button", { name: "View full message" }));
  const signal = fetch.mock.calls[0][1]!.signal!;
  await act(async () => {
    useSessionStore.getState().setMessages("s", [complete(100)]);
    // No React commit/effect has run at this point.
    expect(signal.aborted).toBe(true);
    finish(Response.json(complete(1)));
  });
  expect(view.getByText("Preview 1")).toBeVisible();
  expect(view.queryByText("Complete 1")).not.toBeInTheDocument();
  expect(view.queryByRole("status")).not.toBeInTheDocument();
  fireEvent.click(view.getByRole("button", { name: "View full message" }));
  await act(async () => { finish(Response.json(complete(1))); });
  expect(view.getByText("Complete 1")).toBeVisible();
});

it("keeps a valid full read and body through continuous window updates without another GET", async () => {
  useSessionStore.getState().setMessages("s", [preview(1)]);
  const original = useSessionStore.getState().messageWindows.s;
  let finish!: (response: Response) => void;
  const fetch = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => new Promise<Response>((resolve) => { finish = resolve; }));
  vi.stubGlobal("fetch", fetch);
  const view = render(<Subject message={preview(1)} commits={[]} />);
  fireEvent.click(view.getByRole("button", { name: "View full message" }));
  act(() => useSessionStore.getState().setMessages("s", [preview(1), complete(2)]));
  expect(useSessionStore.getState().messageWindows.s).toBe(original);
  expect(fetch.mock.calls[0][1]!.signal!.aborted).toBe(false);
  await act(async () => { finish(Response.json(complete(1))); });
  act(() => useSessionStore.getState().setMessages("s", [preview(1), complete(2), complete(3)]));
  expect(view.getByText("Complete 1")).toBeVisible();
  fireEvent.click(view.getByRole("button", { name: "Show preview" }));
  fireEvent.click(view.getByRole("button", { name: "View full message" }));
  expect(view.getByText("Complete 1")).toBeVisible();
  expect(fetch).toHaveBeenCalledTimes(1);
});

it.each(["reference", "session", "account", "window"])("never fills a new %s resource with an already loaded body", async (boundary) => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(complete(1))));
  const commits: string[] = [];
  const view = render(<Subject message={preview(1)} commits={commits} />);
  fireEvent.click(view.getByRole("button", { name: "View full message" }));
  expect(await view.findByText("Complete 1")).toBeVisible();
  commits.length = 0;
  if (boundary === "reference") view.rerender(<Subject message={{ ...preview(1), detail_ref: undefined }} commits={commits} />);
  else if (boundary === "session") view.rerender(<Subject sessionId="other" message={{ ...preview(1), detail_ref: "/api/sessions/other/messages/1" }} commits={commits} />);
  else act(() => {
    if (boundary === "account") useSessionStore.setState({ accountGeneration: 2 });
    else useSessionStore.getState().setMessages("s", [complete(100)]);
  });
  expect(commits[0]).toContain("Preview 1");
  expect(commits[0]).not.toContain("Complete 1");
  expect(view.queryByText("Complete 1")).not.toBeInTheDocument();
});
