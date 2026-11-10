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
  useSessionStore.setState({ token: "owned", accountGeneration: 1, connected: true, dispatchGeneration: "same-service", messages: {}, lastAppliedSeq: { s: 10 } });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

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
  expect(fetch.mock.calls[1][0]).toBe("/api/sessions/s/messages/2");
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

test.each(["refresh", "reconnect", "target", "reopen"])("%s invalidates a late page even on the same service", async (change) => {
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
