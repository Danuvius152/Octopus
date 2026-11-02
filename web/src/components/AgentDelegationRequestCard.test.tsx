/** Readable delegation cards associate only with their canonical call and round receipt. */
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

import { AgentDelegationRequestCard } from "./AgentDelegationRequestCard";
import { MessageBubble } from "./MessageBubble";
import { handleWsMessage } from "../hooks/useWebSocket";
import { observeDelegations } from "../lib/delegationReads";
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
  const reconnected = observeDelegations("parent");
  await act(async () => replies[0](Response.json([delegation()])));
  await act(async () => {
    replies[1](Response.json([delegation({ state: "completed" })]));
    await reconnected.done;
  });
  expect(screen.getByText("completed")).toBeTruthy();
  expect(screen.queryByTitle("Cancel delegation")).toBeNull();
});
