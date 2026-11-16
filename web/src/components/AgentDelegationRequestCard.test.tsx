/** Readable delegation cards associate only with their canonical call and round receipt. */
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

import { AgentDelegationRequestCard } from "./AgentDelegationRequestCard";
import { MessageBubble } from "./MessageBubble";
import { handleWsMessage } from "../hooks/useWebSocket";
import { refreshDelegations } from "../lib/delegationReads";
import { observeSession } from "../lib/hydrateSession";
import { useSessionStore, type Delegation, type Message, type SessionInfo } from "../stores/sessionStore";

const metadata = (call = "call-current", work = "round-current", receipt = true) => ({
  request_id: `request-${call}`, attempt_id: `attempt-${call}`, tool_call_id: call,
  receipt: receipt ? { kind: "delegation" as const, delegation_id: "child", work_id: work } : null,
});
const toolUse = (call = "call-current"): Message => ({
  role: "assistant", type: "tool_use", tool_use_id: "item_0", tool_name: "mcp__ask_agent__ask",
  tool_input: { name: "Vera", request: "review the readme" }, runtime_metadata: metadata(call, "", false),
});
const result = (call = "call-current", work = "round-current"): Message => ({
  role: "tool", type: "tool_result", tool_use_id: "item_0", runtime_metadata: metadata(call, work),
  content: "Readable delegation accepted receipt",
});
const delegation = (overrides: Partial<Delegation> = {}): Delegation => ({
  delegation_id: "child", sub_session_id: "child", parent_session_id: "parent", work_id: "round-current",
  target_agent_id: "vera-agent", target_agent_name: "Vera", request: "review the readme", state: "running",
  created_at: "2026-10-14T00:00:00Z", finished_at: null, error: null, ...overrides,
});
const card = (message = toolUse(), agentName = "Vera", files?: string[]) => <AgentDelegationRequestCard
  sessionId="parent" message={message} agentName={agentName} request="review the readme" files={files} />;

beforeEach(() => {
  useSessionStore.setState({ token: "token", sessions: [], delegations: {}, messages: {}, activeAgentId: null, activeSessionId: null });
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify([delegation()]))));
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it.each([false, true])("shares transport across two cards without borrowing failed hydration (held detail=%s)", async (held) => {
  vi.useFakeTimers();
  const replies: ((response: Response) => void)[] = [];
  const rows = [delegation(), delegation({ work_id: "other-work", target_agent_name: "Weber" })];
  let finishDetail!: (response: Response) => void;
  const detailResponse = new Promise<Response>((resolve) => { finishDetail = resolve; });
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (url.endsWith("/delegations")) return new Promise<Response>((resolve) => replies.push(resolve));
    if (url.includes("?view=browser")) return detailResponse;
    return Promise.resolve(Response.json([]));
  }));
  useSessionStore.getState().setMessages("parent", [toolUse(), result(), toolUse("other-call"), result("other-call", "other-work")]);
  useSessionStore.getState().setDelegations("parent", rows);
  render(<>{card()}{card(toolUse("other-call"), "Weber")}</>);
  const hydration = observeSession("parent");
  const failed = expect(hydration.done).rejects.toThrow("HTTP 503");
  try {
    if (!held) { finishDetail(Response.json({}, { status: 503 })); await failed; }
    expect(replies).toHaveLength(1);
    await act(async () => replies[0](Response.json(rows)));
    expect(screen.getAllByText("running")).toHaveLength(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(replies).toHaveLength(2);
    if (held) { finishDetail(Response.json({}, { status: 503 })); await failed; }
    await act(async () => replies[1](Response.json(rows.map((row) => ({ ...row, state: "completed" })))));
    expect(screen.getAllByText("completed")).toHaveLength(2);
  } finally { hydration.cancel(); }
});

it("issues confirmation after an unknown cancel while the older ordinary GET is still in flight", async () => {
  const replies: ((response: Response) => void)[] = [];
  const events: string[] = [];
  vi.stubGlobal("fetch", vi.fn((_url: string, init?: RequestInit) => {
    events.push(init?.method || "GET");
    if (init?.method === "POST") return Promise.reject(new Error("ACK lost"));
    return new Promise<Response>((resolve) => replies.push(resolve));
  }));
  useSessionStore.getState().setMessages("parent", [toolUse(), result()]);
  useSessionStore.getState().setDelegations("parent", [delegation()]);
  render(card());
  await act(async () => fireEvent.click(screen.getByTitle("Cancel delegation")));
  expect(screen.getByRole("alert")).toHaveTextContent("unconfirmed");
  await act(async () => fireEvent.click(screen.getByText("Refresh status")));
  expect(events).toEqual(["GET", "POST", "GET"]);
  await act(async () => replies[1](Response.json([delegation({ state: "cancelled" })])));
  await act(async () => replies[0](Response.json([delegation()])));
  expect(screen.getByText("cancelled")).toBeTruthy();
  expect(events.filter((method) => method === "POST")).toHaveLength(1);
});

it.each([false, true])("does not confirm a later receipt's absence with an older GET (live row=%s)", async (liveRow) => {
  vi.useFakeTimers();
  const replies: ((response: Response) => void)[] = [];
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => replies.push(resolve))));
  useSessionStore.getState().setMessages("parent", [toolUse(), result()]);
  const view = render(card());
  await act(async () => {
    useSessionStore.getState().setMessages("parent", [toolUse(), result(), toolUse("new-call"), result("new-call", "new-work")]);
    if (liveRow) handleWsMessage({ type: "delegation_delivery", session_id: "parent", delegation: delegation({ work_id: "new-work" }) });
  });
  view.rerender(<>{card()}{card(toolUse("new-call"))}</>);
  expect(replies).toHaveLength(1);
  await act(async () => replies[0](Response.json([])));
  // The original missing work is finished. Only the newly joined receipt can
  // keep this coordinator observing until a request started after it joined.
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(replies).toHaveLength(2);
  await act(async () => replies[1](Response.json([delegation({ work_id: "new-work", state: "completed" })])));
  expect(screen.getByText("completed")).toBeTruthy();
  expect(screen.getByText("status unavailable")).toBeTruthy();
});

it("ends only the missing exact-work demand while the other work continues refreshing", async () => {
  vi.useFakeTimers();
  const replies: ((response: Response) => void)[] = [];
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => replies.push(resolve))));
  useSessionStore.getState().setMessages("parent", [toolUse(), result(), toolUse("other-call"), result("other-call", "other-work")]);
  const other = delegation({ work_id: "other-work", target_agent_name: "Weber" });
  const view = render(<>{card()}{card(toolUse("other-call"), "Weber")}</>);
  await act(async () => replies[0](Response.json([other])));
  expect(screen.getByText("status unavailable")).toBeTruthy();
  expect(screen.getByText("running")).toBeTruthy();
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(replies).toHaveLength(2);
  await act(async () => replies[1](Response.json([other, delegation({ work_id: "new-round" })])));
  expect(screen.getByText("status unavailable")).toBeTruthy();
  view.rerender(card());
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(replies).toHaveLength(2);
});

it("keeps one failure budget across cards, pauses after three failures and offers a fresh explicit read", async () => {
  vi.useFakeTimers();
  let calls = 0;
  vi.stubGlobal("fetch", vi.fn(async () => {
    calls++;
    if (calls < 3) return Response.json({ detail: "Offline" }, { status: 503 });
    if (calls === 3) return Response.json({ invalid: "collection" });
    return Response.json([delegation({ state: "completed" }), delegation({ work_id: "other-work", state: "completed" })]);
  }));
  useSessionStore.getState().setMessages("parent", [toolUse(), result(), toolUse("other-call"), result("other-call", "other-work")]);
  useSessionStore.getState().setDelegations("parent", [delegation(), delegation({ work_id: "other-work" })]);
  const view = render(card());
  await act(async () => {});
  expect(calls).toBe(1);
  view.rerender(<>{card()}{card(toolUse("other-call"), "Weber")}</>);
  expect(calls).toBe(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
  expect(calls).toBe(3);
  expect(screen.getAllByRole("alert")).toHaveLength(2);
  view.rerender(<>{card()}{card(toolUse("other-call"), "Weber")}{card()}</>);
  expect(calls).toBe(3);
  expect(screen.getAllByRole("alert")).toHaveLength(3);
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(calls).toBe(3);
  await act(async () => fireEvent.click(screen.getAllByText("Refresh status")[0]));
  expect(calls).toBe(4);
  expect(screen.getAllByText("completed")).toHaveLength(3);
});

it("shows a later exhausted observation even after a successful explicit refresh of running work", async () => {
  vi.useFakeTimers();
  let gets = 0;
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "POST") return Response.json({});
    return ++gets <= 2 ? Response.json([delegation()]) : Response.json({}, { status: 503 });
  }));
  useSessionStore.getState().setMessages("parent", [toolUse(), result()]);
  useSessionStore.getState().setDelegations("parent", [delegation()]);
  render(card());
  await act(async () => {});
  await act(async () => fireEvent.click(screen.getByTitle("Cancel delegation")));
  await act(async () => fireEvent.click(screen.getByText("Refresh status")));
  expect(screen.getByText("Current status refreshed.")).toBeTruthy();
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
  expect(gets).toBe(5);
  expect(screen.getByRole("alert")).toHaveTextContent("could not be confirmed");
  expect(screen.getAllByText("Refresh status")).toHaveLength(1);
});

it.each(["completed", "cancelled"] as const)("clears an exhausted observation warning when WS confirms %s", async (state) => {
  vi.useFakeTimers();
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({}, { status: 503 })));
  useSessionStore.getState().setMessages("parent", [toolUse(), result()]);
  useSessionStore.getState().setDelegations("parent", [delegation()]);
  render(card());
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
  expect(screen.getByRole("alert")).toHaveTextContent("could not be confirmed");
  await act(async () => handleWsMessage({ type: "delegation_delivery", session_id: "parent", delegation: delegation({ state }) }));
  expect(screen.getByText(state)).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.queryByText("Refresh status")).toBeNull();
});

it("shows a failed exact-round cancel while retaining the original running work", async () => {
  useSessionStore.getState().setMessages("parent", [toolUse(), result()]);
  useSessionStore.getState().setDelegations("parent", [delegation()]);
  vi.stubGlobal("fetch", vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) =>
    init?.method === "POST" ? Response.json({ detail: "Round control unavailable" }, { status: 503 }) : Response.json([delegation()])));
  render(card());
  fireEvent.click(screen.getByTitle("Cancel delegation"));
  expect(await screen.findByRole("alert")).toHaveTextContent("Round control unavailable");
  expect(useSessionStore.getState().delegations.parent[0]).toMatchObject({ state: "running", work_id: "round-current" });
});

it("shows a running exact round and sends its captured work on cancellation", async () => {
  useSessionStore.getState().setMessages("parent", [toolUse(), result()]);
  useSessionStore.getState().setDelegations("parent", [delegation()]);
  render(card());
  expect(screen.getByText("Asked Vera")).toBeTruthy();
  expect(screen.getByText("running")).toBeTruthy();
  await act(async () => fireEvent.click(screen.getByTitle("Cancel delegation")));
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/api/sessions/parent/delegations/child/cancel"),
    expect.objectContaining({ method: "POST", body: JSON.stringify({ reason: "cancelled from UI", work_id: "round-current" }) }));
});

it.each([["completed", "completed"], ["failed", "failed"], ["cancelled", "cancelled"]] as const)(
  "shows the exact %s terminal without a cancel control", (state, label) => {
    useSessionStore.getState().setMessages("parent", [toolUse(), result()]);
    useSessionStore.getState().setDelegations("parent", [delegation({ state })]);
    render(card());
    expect(screen.getByText(label)).toBeTruthy();
    expect(screen.queryByTitle("Cancel delegation")).toBeNull();
  },
);

it.each([
  ["unknown", "Return admission unconfirmed"],
  ["stopped", "Automatic return stopped"],
] as const)("shows completed business independently of %s parent return", (disposition, label) => {
  useSessionStore.getState().setMessages("parent", [toolUse(), result()]);
  useSessionStore.getState().setDelegations("parent", [delegation()]);
  handleWsMessage({ type: "delegation_delivery", session_id: "parent", delegation: delegation({
    state: "completed", terminal_disposition: disposition, terminal_delivery_error: "Original return reason",
  }) });
  render(card());
  expect(screen.getByText("completed")).toBeTruthy();
  expect(screen.getByText(label)).toBeTruthy();
  expect(screen.getByText("Original return reason")).toBeTruthy();
  expect(screen.queryByTitle("Cancel delegation")).toBeNull();
});

it.each([
  ["unknown", "Return admission unconfirmed"],
  ["stopped", "Automatic return stopped"],
  ["closed", "Automatic return stopped"],
  ["unbound", "Automatic return stopped"],
  ["unavailable", "Automatic return stopped"],
  ["not_replayed", "Automatic return not replayed"],
] as const)("shows retained %s disposition without requiring an error string", (disposition, label) => {
  useSessionStore.getState().setMessages("parent", [toolUse(), result()]);
  useSessionStore.getState().setDelegations("parent", [delegation({
    state: "completed", terminal_disposition: disposition, terminal_delivery_error: null,
  })]);
  render(card());
  expect(screen.getByText("completed")).toBeTruthy();
  expect(screen.getByText(label)).toBeTruthy();
});

it("uses the receipt even when readable names, request text and result text differ", () => {
  const accepted = result();
  accepted.content = "Started delegation `wrong-child` to another agent. work_id=wrong-round";
  useSessionStore.getState().setMessages("parent", [toolUse(), accepted]);
  useSessionStore.getState().setDelegations("parent", [
    delegation({ delegation_id: "wrong-child", work_id: "wrong-round" }),
    delegation({ target_agent_name: "Vera", request: "normalized request", state: "completed" }),
  ]);
  render(card(toolUse(), "model-spelled-name"));
  expect(screen.getByText("Asked Vera")).toBeTruthy();
  expect(screen.getByText("completed")).toBeTruthy();
  expect(screen.queryByTitle("Cancel delegation")).toBeNull();
});

it("keeps repeated provider IDs and an earlier round away from a running continuation", async () => {
  useSessionStore.getState().setMessages("parent", [toolUse("call-old"), result("call-old", "round-old"), toolUse(), result()]);
  useSessionStore.getState().setDelegations("parent", [delegation(), delegation({ work_id: "round-old", state: "completed" })]);
  const { container } = render(<>{card(toolUse("call-old"))}{card()}</>);
  const cards = container.querySelectorAll(".agent-delegation-request");
  expect(cards[0]).toHaveAttribute("data-delegation-state", "completed");
  expect(cards[1]).toHaveAttribute("data-delegation-state", "running");
  expect(screen.getAllByTitle("Cancel delegation")).toHaveLength(1);
  await act(async () => {});
});

it("keeps an interrupted round readable without letting it cancel the new continuation", async () => {
  const previous = toolUse("call-before-restart");
  const continued = { ...toolUse(), tool_input: { delegation_id: "child", request: "review again" } };
  const records = [
    delegation({ work_id: "round-before-restart", state: "interrupted", finished_at: "2026-10-14T00:01:00Z", error: "Service shutdown interrupted the delegation" }),
    delegation({ request: "review again" }),
  ];
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(records))));
  useSessionStore.getState().setMessages("parent", [previous, result("call-before-restart", "round-before-restart"), continued, result()]);
  useSessionStore.getState().setDelegations("parent", records);
  await act(async () => {
    render(<><MessageBubble sessionId="parent" message={previous} /><MessageBubble sessionId="parent" message={continued} /></>);
  });

  expect(screen.getByText("interrupted")).toBeTruthy();
  expect(screen.getByText("“review the readme”")).toBeTruthy();
  expect(screen.getByText("Continued Vera")).toBeTruthy();
  expect(screen.getByText("“review again”")).toBeTruthy();
  expect(screen.getAllByTitle("Cancel delegation")).toHaveLength(1);
  await act(async () => fireEvent.click(screen.getByTitle("Cancel delegation")));
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/api/sessions/parent/delegations/child/cancel"),
    expect.objectContaining({ method: "POST", body: JSON.stringify({ reason: "cancelled from UI", work_id: "round-current" }) }));
  expect(screen.getByText("interrupted")).toBeTruthy();
});

it("renders the continuation without requiring a name argument", () => {
  const continued = { ...toolUse(), tool_input: { delegation_id: "child", request: "review again" } };
  useSessionStore.getState().setMessages("parent", [continued, result()]);
  useSessionStore.getState().setDelegations("parent", [delegation({ state: "completed" })]);
  render(<MessageBubble sessionId="parent" message={continued} />);
  expect(screen.getByText("Continued Vera")).toBeTruthy();
  expect(screen.getByText("“review again”")).toBeTruthy();
  expect(screen.getByText("completed")).toBeTruthy();
});

it("waits for a receipt without borrowing a name/request match or claiming running", () => {
  useSessionStore.getState().setDelegations("parent", [delegation()]);
  render(card());
  expect(screen.getByText("awaiting receipt")).toBeTruthy();
  expect(screen.queryByText("running")).toBeNull();
  expect(screen.queryByTitle("Cancel delegation")).toBeNull();
  expect(fetch).not.toHaveBeenCalled();
});

it("keeps a failed call readable without trusting its receipt", () => {
  useSessionStore.getState().setMessages("parent", [toolUse(), { ...result(), is_error: true, content: "Delegation refused" }]);
  useSessionStore.getState().setDelegations("parent", [delegation()]);
  render(card());
  expect(screen.getByText("failed to start")).toBeTruthy();
  expect(screen.queryByTitle("Cancel delegation")).toBeNull();
});

it("reports unavailable exact-round state and stops polling a missing round", async () => {
  vi.useFakeTimers();
  useSessionStore.getState().setMessages("parent", [toolUse("call-old"), result("call-old", "round-no-longer-retained")]);
  useSessionStore.getState().setDelegations("parent", [delegation()]);
  render(card(toolUse("call-old")));
  expect(screen.getByText("status unavailable")).toBeTruthy();
  expect(screen.queryByText("running")).toBeNull();
  expect(screen.queryByTitle("Cancel delegation")).toBeNull();
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("opens the receipt's child and keeps the task brief and files readable", async () => {
  useSessionStore.setState({ sessions: [{ id: "child", agent_id: "vera-agent" } as SessionInfo] });
  useSessionStore.getState().setMessages("parent", [toolUse(), result()]);
  useSessionStore.getState().setDelegations("parent", [delegation({ state: "completed" })]);
  render(card(toolUse(), "Vera", ["a.tsx", "b.tsx"]));
  expect(screen.getByText("“review the readme”")).toBeTruthy();
  expect(screen.getByText("files: a.tsx, b.tsx")).toBeTruthy();
  await act(async () => fireEvent.click(screen.getByTitle("Open Vera's session")));
  expect(useSessionStore.getState().activeSessionId).toBe("child");
  expect(useSessionStore.getState().activeAgentId).toBe("vera-agent");
});

it("accepts a completed round after its polling success while another list read overlaps", async () => {
  const replies: ((response: Response) => void)[] = [];
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => replies.push(resolve))));
  useSessionStore.getState().setMessages("parent", [toolUse(), result()]);
  useSessionStore.getState().setDelegations("parent", [delegation()]);
  render(card());
  const reconnected = refreshDelegations("parent");
  await act(async () => replies[0](Response.json([delegation()])));
  await act(async () => {
    replies[1](Response.json([delegation({ state: "completed" })]));
    await reconnected.done;
  });
  expect(screen.getByText("completed")).toBeTruthy();
  expect(screen.queryByTitle("Cancel delegation")).toBeNull();
});
