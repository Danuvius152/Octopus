import { useLayoutEffect, useRef } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { NativeSubagentDetails } from "./NativeSubagentDetails";
import { useSessionStore } from "../stores/sessionStore";

const page = (text: string, anchor = 1, seq = 2) => ({ anchor_seq: anchor,
  child: { task_id: "child", native_thread_id: "child", native_turn_id: "turn", status: "interrupted", stop_reason: "parent_done" },
  availability: "available", messages: [{ role: "assistant", type: "text", content: text, seq }],
  next_after_seq: seq, has_more: false });
const respond = (value: unknown) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });

beforeEach(() => {
  useSessionStore.setState({ token: "owned", accountGeneration: 1, connected: true, dispatchGeneration: "same-service", messages: {},
    messageWindows: { s: Symbol("original window") }, lastAppliedSeq: { s: 10 } });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

// A layout-time snapshot of the public reader catches stale content before
// passive effects can hide it. No reader internals are exposed to the test.
function NativeView({ anchorSeq, commits }: { anchorSeq: number; commits: string[] }) {
  useSessionStore((state) => state.messageWindows.s);
  const element = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => { commits.push(element.current!.textContent ?? ""); });
  return <div ref={element}><NativeSubagentDetails sessionId="s" anchorSeq={anchorSeq} /></div>;
}

test.each(["success", "failure", "loading"])("hides an old native reader's %s on the first new-anchor render", async (state) => {
  const responses: ((response: Response) => void)[] = [];
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { responses.push(resolve); })));
  const commits: string[] = [];
  const view = render(<NativeView anchorSeq={1} commits={commits} />);
  expect(screen.getByRole("status")).toHaveTextContent("Loading saved details");
  if (state !== "loading") await act(async () => {
    responses[0](state === "success" ? respond(page("Old saved body")) : new Response("Old failure", { status: 503 }));
  });
  if (state === "success") expect(screen.getByText("Old saved body")).toBeVisible();
  if (state === "failure") expect(screen.getByRole("alert")).toHaveTextContent("503");
  commits.length = 0;
  view.rerender(<NativeView anchorSeq={7} commits={commits} />);
  expect(commits[0]).not.toMatch(/Old saved body|503|Loading saved details/);
  expect(responses).toHaveLength(2);
  if (state === "loading") await act(async () => { responses[0](respond(page("Retired saved body"))); });
  expect(screen.getByRole("status")).toHaveTextContent("Loading saved details");
  expect(screen.queryByText("Retired saved body")).not.toBeInTheDocument();
  await act(async () => { responses[1](respond(page("Current saved body", 7))); });
  expect(screen.getByText("Current saved body")).toBeVisible();
});

test("saved details render separately from the main transcript and leave its high water alone", async () => {
  const fetch = vi.fn().mockResolvedValue(respond(page("Saved partial")));
  vi.stubGlobal("fetch", fetch);
  render(<NativeSubagentDetails sessionId="s" anchorSeq={1} />);
  expect(await screen.findByText("Saved partial")).toBeInTheDocument();
  expect(screen.getByText(/parent has consumed/i)).toBeInTheDocument();
  expect(fetch.mock.calls[0][0]).toContain("/api/sessions/s/native-details/1");
  expect(useSessionStore.getState().messages).toEqual({});
  expect(useSessionStore.getState().lastAppliedSeq.s).toBe(10);
});

test("saved helper tool calls show arguments without raw audit data", async () => {
  const parameters = { pattern: "original input", id: "business-id", nullable: null, request_context: { request_id: "business-request" } };
  const input = { ...parameters, native_item: { type: "mcpToolCall", id: "native-audit-id", arguments: parameters } };
  const fetch = vi.fn().mockResolvedValue(respond({ ...page(""), messages: [{ role: "assistant", type: "tool_use",
    tool_name: "mcp__wiki__search", tool_input: input, seq: 2, runtime_metadata: { request_id: "request", attempt_id: "attempt",
      native: { thread_id: "child", turn_id: "turn", root_thread_id: "root", root_turn_id: "turn", item_id: "native-audit-id" } } }] }));
  vi.stubGlobal("fetch", fetch);
  render(<NativeSubagentDetails sessionId="s" anchorSeq={1} />);
  const primary = await screen.findByText(/"pattern": "original input"/, { selector: ".tool-detail" });
  expect(primary.textContent).toBe(JSON.stringify(parameters, null, 2));
  expect(screen.queryByText("Raw tool data", { exact: true })).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Raw tool input")).not.toBeInTheDocument();
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(useSessionStore.getState().messages).toEqual({});
});

test("a collected preview expands its original full message without fetching unrelated bodies", async () => {
  const first = page("Child preview…");
  const fetch = vi.fn().mockResolvedValueOnce(respond({ ...first, messages: first.messages.map((message) => ({
    ...message, body_state: "preview", detail_ref: "/api/sessions/s/messages/2" })) }))
    .mockResolvedValueOnce(respond({ role: "assistant", type: "text", seq: 2, content: "Complete collected child body",
      tool_input: { native_item: { type: "agentMessage", text: "Complete collected child body" },
        request_context: { source: "human", content: "Complete collected child body" } } }));
  vi.stubGlobal("fetch", fetch);
  render(<NativeSubagentDetails sessionId="s" anchorSeq={1} />);
  expect(await screen.findByText("Child preview…")).toBeVisible();
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0][0]).toContain("view=browser");
  fireEvent.click(screen.getByRole("button", { name: "View full message" }));
  expect(await screen.findByText("Complete collected child body")).toBeVisible();
  expect(screen.getAllByText(/Complete collected child body/)).toHaveLength(1);
  expect(fetch.mock.calls[1][0]).toBe("/api/sessions/s/messages/2?view=browser");
  expect(useSessionStore.getState().messages).toEqual({});
});

test("an unavailable native association expands original identity and content without changing live state", async () => {
  const first = page("Unavailable original association");
  const identity = { request_id: "r", attempt_id: "a", native: {
    thread_id: "Original native helper identity", turn_id: "turn", root_thread_id: "root", root_turn_id: "turn" } };
  const fetcher = vi.fn().mockResolvedValueOnce(respond({ ...first,
    child: { ...first.child, native_thread_id: identity.native.thread_id },
    messages: [{ role: "system", type: "notice", seq: 2, content: "Unavailable original association",
      body_state: "preview", identity_unavailable: true, detail_ref: "/api/sessions/s/messages/2" }] }))
    .mockResolvedValueOnce(respond({ role: "assistant", type: "text", seq: 2,
      content: "Complete original child content", runtime_metadata: identity }));
  vi.stubGlobal("fetch", fetcher);
  const live = useSessionStore.getState().subagents;
  render(<NativeSubagentDetails sessionId="s" anchorSeq={1} />);
  fireEvent.click(await screen.findByRole("button", { name: "View full message" }));
  expect(await screen.findByText(/Complete original child content/)).toBeVisible();
  expect(screen.getByText(/Original native helper identity/)).toBeVisible();
  expect(screen.queryByRole("button", { name: "Submit" })).not.toBeInTheDocument();
  expect(useSessionStore.getState().messages).toEqual({});
  expect(useSessionStore.getState().subagents).toBe(live);
});

test.each(["refresh", "reconnect", "target", "reopen", "account", "service"])("%s invalidates a late page", async (change) => {
  let resolveOld!: (response: Response) => void;
  const fetch = vi.fn().mockImplementationOnce(() => new Promise<Response>((resolve) => { resolveOld = resolve; }))
    .mockImplementation(() => Promise.resolve(respond(page("Current saved body", change === "target" ? 7 : 1))));
  vi.stubGlobal("fetch", fetch);
  const view = render(<NativeSubagentDetails sessionId="s" anchorSeq={1} />);
  if (change === "refresh") fireEvent.click(screen.getByText("Refresh details"));
  else if (change === "reconnect") act(() => {
    useSessionStore.getState().setConnected(false);
    useSessionStore.getState().setConnected(true);
  });
  else if (change === "target") view.rerender(<NativeSubagentDetails sessionId="new-session" anchorSeq={7} />);
  else if (change === "account") act(() => useSessionStore.getState().setToken("new-owned-account"));
  else if (change === "service") act(() => useSessionStore.setState({ dispatchGeneration: "new-service" }));
  else {
    view.unmount();
    render(<NativeSubagentDetails sessionId="s" anchorSeq={1} />);
  }
  expect(await screen.findByText("Current saved body")).toBeInTheDocument();
  await act(async () => { resolveOld(respond(page("Stale old body"))); });
  expect(screen.queryByText("Stale old body")).not.toBeInTheDocument();
  expect(screen.getByText("Current saved body")).toBeInTheDocument();
});

test("uncollected history differs from a failed query and retry only reads saved items", async () => {
  const fetch = vi.fn().mockRejectedValueOnce(new Error("Read failed"))
    .mockResolvedValueOnce(respond({ ...page(""), availability: "not_collected", messages: [] }));
  vi.stubGlobal("fetch", fetch);
  render(<NativeSubagentDetails sessionId="s" anchorSeq={1} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Read failed");
  expect(screen.queryByText(/history was not collected/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByText("Refresh details"));
  expect(await screen.findByText(/history was not collected/)).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(fetch).toHaveBeenCalledTimes(2);
});

test("detail pagination keeps tool use and result sharing a native item and never bills them again", async () => {
  const tool = { role: "assistant", type: "tool_use", seq: 4, tool_name: "Bash", tool_input: { command: "true" } };
  const fetch = vi.fn().mockResolvedValueOnce(respond({ ...page(""), messages: [tool], next_after_seq: 4, has_more: true }))
    .mockResolvedValueOnce(respond({ ...page(""), messages: [{ role: "tool", type: "tool_result", seq: 5, content: "Command output" }] }));
  vi.stubGlobal("fetch", fetch);
  render(<NativeSubagentDetails sessionId="s" anchorSeq={1} />);
  fireEvent.click(await screen.findByText("More saved items"));
  expect(await screen.findByText("Command output")).toBeInTheDocument();
  expect(screen.getByText("tool_use · Bash")).toBeInTheDocument();
  expect(fetch.mock.calls[1][0]).toContain("after_seq=4");
  expect(useSessionStore.getState().lastAppliedSeq.s).toBe(10);
});

test("a snapshot with no saved reference never fabricates a detail request", () => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  render(<NativeSubagentDetails sessionId="s" />);
  expect(screen.getByText(/No saved reference/)).toBeInTheDocument();
  expect(fetch).not.toHaveBeenCalled();
});

test("a late next page cannot append into a refreshed view of the same helper", async () => {
  let resolveMore!: (response: Response) => void;
  const fetch = vi.fn().mockResolvedValueOnce(respond({ ...page("First body"), has_more: true }))
    .mockImplementationOnce(() => new Promise<Response>((resolve) => { resolveMore = resolve; }))
    .mockResolvedValueOnce(respond(page("Refreshed body")));
  vi.stubGlobal("fetch", fetch);
  render(<NativeSubagentDetails sessionId="s" anchorSeq={1} />);
  fireEvent.click(await screen.findByText("More saved items"));
  fireEvent.click(screen.getByText("Refresh details"));
  expect(await screen.findByText("Refreshed body")).toBeInTheDocument();
  await act(async () => { resolveMore(respond(page("Old next page", 1, 3))); });
  expect(screen.queryByText("Old next page")).not.toBeInTheDocument();
  expect(screen.queryByText("First body")).not.toBeInTheDocument();
});

test.each(["success", "failure"])("replaces the same-anchor reader on a new window without accepting an old page's %s or finally", async (outcome) => {
  const responses: ((response: Response) => void)[] = [];
  const fetch = vi.fn(() => new Promise<Response>((resolve) => { responses.push(resolve); }));
  vi.stubGlobal("fetch", fetch);
  const commits: string[] = [];
  render(<NativeView anchorSeq={1} commits={commits} />);
  await act(async () => { responses[0](respond({ ...page("Old first page"), has_more: true })); });
  fireEvent.click(screen.getByRole("button", { name: "More saved items" }));
  expect(responses).toHaveLength(2);
  const oldWindow = useSessionStore.getState().messageWindows.s;
  commits.length = 0;
  act(() => useSessionStore.getState().setMessages("s", [{ role: "assistant", type: "text", seq: 100, content: "Replacement window" }]));
  expect(useSessionStore.getState().messageWindows.s).not.toBe(oldWindow);
  expect(commits[0], "the first replacement-window commit excludes the retired page").not.toContain("Old first page");
  expect(responses).toHaveLength(3);
  expect(screen.getByRole("status")).toHaveTextContent("Loading saved details");
  await act(async () => {
    responses[1](outcome === "success" ? respond(page("Retired next page", 1, 3)) : new Response("Old page failure", { status: 503 }));
  });
  expect(screen.getByRole("status")).toHaveTextContent("Loading saved details");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.queryByText("Retired next page")).not.toBeInTheDocument();
  expect(screen.queryByText("Old first page")).not.toBeInTheDocument();
  await act(async () => { responses[2](respond(page("New window first page"))); });
  expect(screen.getByText("New window first page")).toBeVisible();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

test("preserves an in-flight native read and its page across continuous window updates", async () => {
  const first = { role: "assistant" as const, type: "text", seq: 1, content: "Window row 1" };
  useSessionStore.getState().setMessages("s", [first]);
  const original = useSessionStore.getState().messageWindows.s;
  let finish!: (response: Response) => void;
  const fetch = vi.fn(() => new Promise<Response>((resolve) => { finish = resolve; }));
  vi.stubGlobal("fetch", fetch);
  render(<NativeSubagentDetails sessionId="s" anchorSeq={1} />);
  const next = { ...first, seq: 3, content: "Window row 3" };
  act(() => useSessionStore.getState().setMessages("s", [first, next]));
  expect(useSessionStore.getState().messageWindows.s).toBe(original);
  expect(fetch).toHaveBeenCalledTimes(1);
  await act(async () => { finish(respond(page("Still valid saved page"))); });
  const body = screen.getByText("Still valid saved page");
  act(() => useSessionStore.getState().setMessages("s", [first, next, { ...next, seq: 4 }]));
  expect(screen.getByText("Still valid saved page")).toBe(body);
  expect(fetch).toHaveBeenCalledTimes(1);
});

test("a retired page control cannot start a read after a store-window change before React commit", async () => {
  const responses: ((response: Response) => void)[] = [];
  const fetch = vi.fn(() => new Promise<Response>((resolve) => { responses.push(resolve); }));
  vi.stubGlobal("fetch", fetch);
  render(<NativeSubagentDetails sessionId="s" anchorSeq={1} />);
  await act(async () => { responses[0](respond({ ...page("Old page"), has_more: true })); });
  const more = screen.getByRole("button", { name: "More saved items" });
  act(() => {
    useSessionStore.getState().setMessages("s", [{ role: "assistant", type: "text", seq: 100, content: "New window" }]);
    // A controlled delivery through the still-mounted public button, while
    // the store has changed and React's replacement commit is still pending.
    more.click();
    expect(fetch, "an old page cannot begin a GET in the replacement scope").toHaveBeenCalledTimes(1);
  });
  expect(fetch).toHaveBeenCalledTimes(2);
  await act(async () => { responses[1](respond(page("Current window page"))); });
  expect(screen.getByText("Current window page")).toBeVisible();
  expect(screen.queryByText("Old page")).not.toBeInTheDocument();
});

test.each(["reconnect", "refresh"])("%s permanently retires a More control before the successor commit", async (change) => {
  const responses: ((response: Response) => void)[] = [];
  const fetch = vi.fn<typeof globalThis.fetch>(() => new Promise<Response>((resolve) => { responses.push(resolve); }));
  vi.stubGlobal("fetch", fetch);
  render(<NativeSubagentDetails sessionId="s" anchorSeq={1} />);
  await act(async () => { responses[0](respond({ ...page("Previous reader page"), has_more: true })); });
  const more = screen.getByRole("button", { name: "More saved items" });
  act(() => {
    if (change === "reconnect") {
      useSessionStore.getState().setConnected(false);
      useSessionStore.getState().setConnected(true);
    } else screen.getByRole("button", { name: "Refresh details" }).click();
    // Scope fields match their original values, but React has not replaced
    // this retired reader or its public page control yet.
    more.click();
    expect(fetch, "a retired reader cannot restart even when scope fields return to their old values").toHaveBeenCalledTimes(1);
  });
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(fetch.mock.calls[1][0]).not.toContain("after_seq=");
  expect(screen.queryByText("Previous reader page")).not.toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Loading saved details");
  await act(async () => { responses[1](respond({ ...page("Successor reader page", 1, 7), has_more: true })); });
  fireEvent.click(screen.getByRole("button", { name: "More saved items" }));
  expect(fetch).toHaveBeenCalledTimes(3);
  expect(fetch.mock.calls[2][0]).toContain("after_seq=7");
  expect(screen.getByRole("status")).toHaveTextContent("Loading saved details");
  await act(async () => { responses[2](respond(page("Current next page", 1, 8))); });
  expect(screen.getByText("Successor reader page")).toBeVisible();
  expect(screen.getByText("Current next page")).toBeVisible();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});
