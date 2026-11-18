import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AgentDelegationRequestCard } from "./AgentDelegationRequestCard";
import { observeDelegations } from "../lib/delegationReads";
import { useSessionStore, type Delegation, type Message } from "../stores/sessionStore";

const tool: Message = { role: "assistant", type: "tool_use", tool_name: "mcp__ask_agent__ask", tool_use_id: "call",
  runtime_metadata: { request_id: "request", attempt_id: "attempt", tool_call_id: "canonical-call" } };
const receipt: Message = { role: "tool", type: "tool_result", tool_use_id: "call",
  runtime_metadata: { ...tool.runtime_metadata!, receipt: { kind: "delegation", delegation_id: "child", work_id: "round" } } };
const round: Delegation = { delegation_id: "child", sub_session_id: "child", parent_session_id: "parent", work_id: "round",
  target_agent_id: "vera", target_agent_name: "Vera", request: "Review", state: "running", created_at: "now", finished_at: null, error: null };
const card = <AgentDelegationRequestCard sessionId="parent" message={tool} agentName="Vera" request="Review" files={[]} />;
let hidden = false;

beforeEach(() => {
  vi.useFakeTimers();
  hidden = false;
  vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
  useSessionStore.setState({ ...useSessionStore.getInitialState(), token: "account", dispatchGeneration: "service",
    activeSessionId: "parent", mainView: "chat", messages: { parent: [tool, receipt] }, delegations: { parent: [round] } }, true);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("releases hidden cards' shared refresh demand and coalesces their visible return into one read", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json([round])));
  const view = render(<>{card}{card}</>);
  await act(async () => {});
  expect(fetch).toHaveBeenCalledTimes(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(fetch).toHaveBeenCalledTimes(2);

  act(() => { hidden = true; fireEvent(document, new Event("visibilitychange")); });
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
  expect(fetch).toHaveBeenCalledTimes(2);

  await act(async () => { hidden = false; fireEvent(document, new Event("visibilitychange")); fireEvent(document, new Event("visibilitychange")); });
  expect(fetch).toHaveBeenCalledTimes(3);
  expect(screen.getAllByText("running")).toHaveLength(2);
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(fetch).toHaveBeenCalledTimes(4);
  view.unmount();
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
  expect(fetch).toHaveBeenCalledTimes(4);
});

it("starts no continuous demand until both the document and chat panel are visible", async () => {
  hidden = true;
  useSessionStore.setState({ mainView: "monitor" });
  vi.stubGlobal("fetch", vi.fn(async () => Response.json([round])));
  render(card);
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
  expect(fetch).not.toHaveBeenCalled();
  act(() => { hidden = false; fireEvent(document, new Event("visibilitychange")); });
  expect(fetch).not.toHaveBeenCalled();
  await act(async () => { useSessionStore.getState().showChat(); });
  expect(fetch).toHaveBeenCalledTimes(1);
  act(() => { useSessionStore.getState().openManage("monitor"); });
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
  expect(fetch).toHaveBeenCalledTimes(1);
  await act(async () => { useSessionStore.getState().showChat(); });
  expect(fetch).toHaveBeenCalledTimes(2);
});

it("keeps a shared one-shot reader alive when the final visible card releases its demand", async () => {
  let reply!: (response: Response) => void;
  let signal!: AbortSignal;
  vi.stubGlobal("fetch", vi.fn((_url: string, init: RequestInit) => {
    signal = init.signal!;
    return new Promise<Response>((resolve) => { reply = resolve; });
  }));
  render(card);
  const hydration = observeDelegations("parent");
  act(() => { hidden = true; fireEvent(document, new Event("visibilitychange")); });
  expect(signal.aborted).toBe(false);
  await act(async () => { reply(Response.json([{ ...round, state: "completed" }])); await hydration.done; });
  expect(useSessionStore.getState().delegations.parent[0].state).toBe("completed");
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); hidden = false; fireEvent(document, new Event("visibilitychange")); });
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(screen.getByText("completed")).toBeTruthy();
});

it("lets the original cancellation and its explicit confirmation settle while the document is hidden", async () => {
  let postReply!: (response: Response) => void;
  let confirmReply!: (response: Response) => void;
  let postSignal!: AbortSignal;
  let gets = 0, posts = 0;
  vi.stubGlobal("fetch", vi.fn((_url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      posts++; postSignal = init.signal!;
      return new Promise<Response>((resolve) => { postReply = resolve; });
    }
    return ++gets === 1 ? Promise.resolve(Response.json([round]))
      : new Promise<Response>((resolve) => { confirmReply = resolve; });
  }));
  render(card);
  await act(async () => {});
  act(() => { fireEvent.click(screen.getByTitle("Cancel delegation")); hidden = true; fireEvent(document, new Event("visibilitychange")); });
  expect(postSignal.aborted).toBe(false);
  await act(async () => { postReply(Response.json({})); });
  expect(screen.getByText(/Cancellation requested/)).toBeTruthy();
  act(() => { fireEvent.click(screen.getByText("Refresh status")); });
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
  expect(gets).toBe(2);
  await act(async () => { confirmReply(Response.json([{ ...round, state: "cancelled" }])); });
  expect(screen.getByText("cancelled")).toBeTruthy();
  expect(screen.getByText("Current status refreshed.")).toBeTruthy();
  expect(posts).toBe(1);
  await act(async () => { hidden = false; fireEvent(document, new Event("visibilitychange")); });
  expect(gets).toBe(2);
});

it("rejects a retired hidden flight after visible restoration starts a new read", async () => {
  const replies: ((response: Response) => void)[] = [];
  const signals: AbortSignal[] = [];
  vi.stubGlobal("fetch", vi.fn((_url: string, init: RequestInit) => {
    signals.push(init.signal!);
    return new Promise<Response>((resolve) => replies.push(resolve));
  }));
  render(card);
  act(() => { hidden = true; fireEvent(document, new Event("visibilitychange")); });
  expect(signals[0].aborted).toBe(true);
  act(() => { hidden = false; fireEvent(document, new Event("visibilitychange")); });
  expect(replies).toHaveLength(2);
  await act(async () => { replies[1](Response.json([{ ...round, state: "completed" }])); });
  await act(async () => { replies[0](Response.json([round])); });
  expect(screen.getByText("completed")).toBeTruthy();
  expect(useSessionStore.getState().delegations.parent[0].state).toBe("completed");
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
  expect(replies).toHaveLength(2);
});
