import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ResearchCard } from "./ResearchCard";
import { BgTaskChip } from "./BgTaskChip";
import { AgentDelegationRequestCard } from "./AgentDelegationRequestCard";
import { SessionAuxiliaryNotice } from "./SessionAuxiliaryNotice";
import { useSessionStore, type BgTask, type Delegation, type Message, type ResearchJob } from "../stores/sessionStore";

const research: ResearchJob = { id: "task", session_id: "s1", question: "Original research", status: "running", phase: "search" };
const background = { id: "task", session_id: "s1", status: "running", command: "original", stdout: "", stderr: "", exit_code: null } as BgTask;
const delegation = { delegation_id: "child", parent_session_id: "s1", work_id: "task", state: "running", target_agent_name: "Vera" } as Delegation;
const request: Message = { role: "assistant", type: "tool_use", tool_name: "mcp__ask_agent__ask", tool_use_id: "call",
  runtime_metadata: { request_id: "request", attempt_id: "attempt", tool_call_id: "call" } };
const receipt = (work: string): Message => ({ role: "tool", type: "tool_result", tool_use_id: "call",
  runtime_metadata: { request_id: "request", attempt_id: "attempt", tool_call_id: "call",
    receipt: { kind: "delegation", delegation_id: "child", work_id: work } } });
const kinds = ["research", "background", "delegation"] as const;
type Kind = typeof kinds[number];
const card = (kind: Kind, identity = "task") => kind === "research" ? <ResearchCard sessionId="s1" />
  : kind === "background" ? <BgTaskChip sessionId="s1" taskId={identity} />
  : <AgentDelegationRequestCard sessionId="s1" message={request} agentName="Vera" request="Original delegation" files={undefined} />;
const cancelButton = (kind: Kind) => screen.getByTitle(kind === "research" ? "Cancel research" : kind === "background" ? "Cancel this background task" : "Cancel delegation");
const state = (kind: Kind) => {
  const store = useSessionStore.getState();
  return kind === "research" ? store.research.s1[0].status : kind === "background" ? store.bgTasks.s1[0].status : store.delegations.s1[0].state;
};

beforeEach(() => {
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useSessionStore.setState({ token: "token", activeSessionId: "s1", dispatchGeneration: "service",
    research: { s1: [research] }, bgTasks: { s1: [background] }, delegations: { s1: [delegation] }, messages: { s1: [request, receipt("task")] } });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it.each(kinds)("%s shows an unknown network result and confirms through a read without repeating cancel", async (kind) => {
  let completed = false;
  const fetcher = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") throw new TypeError("Connection lost after the server may have cancelled");
    const path = String(url);
    if (path.includes("/research")) return Response.json([{ ...research, status: completed ? "cancelled" : "running" }]);
    if (path.includes("/bg-tasks")) return Response.json({ ...background, status: completed ? "cancelled" : "running" });
    return Response.json([{ ...delegation, state: completed ? "cancelled" : "running" }]);
  });
  vi.stubGlobal("fetch", fetcher);
  render(card(kind));
  fireEvent.click(cancelButton(kind));
  expect(await screen.findByRole("alert")).toHaveTextContent("result is unconfirmed");
  expect(state(kind)).toBe("running");
  completed = true;
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Refresh status" })));
  expect(state(kind)).toBe("cancelled");
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it.each(kinds)("%s suppresses duplicate clicks and keeps a newer terminal after cancel fails", async (kind) => {
  let reply!: (response: Response) => void;
  const fetcher = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => init?.method === "POST"
    ? new Promise<Response>((resolve) => { reply = resolve; }) : Response.json([delegation]));
  vi.stubGlobal("fetch", fetcher);
  render(card(kind));
  fireEvent.click(cancelButton(kind));
  fireEvent.click(cancelButton(kind));
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  await act(async () => {
    const store = useSessionStore.getState();
    if (kind === "research") store.upsertResearch("s1", { id: "task", status: "completed", injection_status: "failed", error: "Original delivery error" });
    else if (kind === "background") store.upsertBgTask("s1", { ...background, status: "completed", delivery_disposition: "unknown", delivery_error: "Original delivery error" });
    else store.upsertDelegation("s1", { ...delegation, state: "completed", terminal_disposition: "unknown", terminal_delivery_error: "Original delivery error" });
    reply(Response.json({ detail: "Control failed after completion" }, { status: 503 }));
  });
  expect(state(kind)).toBe("completed");
  expect(screen.getByRole("alert")).toHaveTextContent("Control failed after completion");
  expect(screen.getByText(/Original delivery error/)).toBeTruthy();
});

for (const kind of kinds) {
  it.each([[200, "unmount"], [503, "unmount"], [200, "identity"], [503, "identity"]] as const)(
    `${kind} cancels an in-flight status read after %s / %s and rejects its late publication`, async (status, change) => {
      let refreshing = false;
      let reply!: (response: Response) => void;
      let signal: AbortSignal | undefined;
      vi.stubGlobal("fetch", vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === "POST") return Response.json({ detail: "Initial control failure" }, { status: 503 });
        if (!refreshing) return Response.json([delegation]);
        signal = init?.signal ?? undefined;
        return new Promise<Response>((resolve) => { reply = resolve; });
      }));
      const view = render(card(kind));
      render(<SessionAuxiliaryNotice sessionId="s1" />);
      fireEvent.click(cancelButton(kind));
      expect(await screen.findByRole("alert")).toHaveTextContent("Initial control failure");
      refreshing = true;
      fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
      await act(async () => {
        if (change === "unmount") view.unmount();
        else useSessionStore.setState({ activeSessionId: "s2" });
      });
      expect(signal?.aborted).toBe(true);
      const data = kind === "research" ? [{ ...research, status: "completed" }]
        : kind === "background" ? { ...background, status: "completed" } : [{ ...delegation, state: "completed" }];
      await act(async () => reply(status === 200 ? Response.json(data) : new Response("Old read failed", { status })));
      expect(state(kind)).toBe("running");
      expect(screen.queryByRole("alert")).toBeNull();
      expect(signal?.aborted).toBe(true);
    },
  );

  it.each(["pending", "failed"])(`${kind} retires old %s feedback across a full disconnect/reconnect`, async (phase) => {
    act(() => useSessionStore.setState({ connected: true }));
    const replies: Array<(response: Response) => void> = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => init?.method === "POST"
      ? new Promise<Response>((resolve) => replies.push(resolve)) : Response.json([delegation])));
    render(card(kind));
    fireEvent.click(cancelButton(kind));
    try {
      if (phase === "failed") {
        await act(async () => replies[0](Response.json({ detail: "Old control error" }, { status: 503 })));
        expect(screen.getByRole("alert")).toHaveTextContent("Old control error");
      }
      act(() => useSessionStore.setState({ connected: false }));
      act(() => useSessionStore.setState({ connected: true }));
      expect(cancelButton(kind)).toBeEnabled();
      expect(screen.queryByRole("alert")).toBeNull();
      fireEvent.click(cancelButton(kind));
      expect(replies).toHaveLength(2);
      await act(async () => replies[1](Response.json(research)));
      await act(async () => replies[0](Response.json({ detail: "Late old error" }, { status: 503 })));
      expect(screen.queryByRole("alert")).toBeNull();
      expect(screen.getByRole("status")).toHaveTextContent("Cancellation requested");
    } finally {
      await act(async () => replies.forEach((reply) => reply(Response.json(research))));
    }
  });

  it.each(["account", "service", "session", "identity", "unmount"])(`${kind} discards feedback after %s changes`, async (change) => {
    let reply!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => init?.method === "POST"
      ? new Promise<Response>((resolve) => { reply = resolve; }) : Response.json([delegation])));
    const view = render(card(kind));
    fireEvent.click(cancelButton(kind));
    await act(async () => {
      if (change === "account") useSessionStore.setState({ accountGeneration: useSessionStore.getState().accountGeneration + 1, token: "new-token" });
      if (change === "service") useSessionStore.setState({ dispatchGeneration: "new-service" });
      if (change === "session") useSessionStore.setState({ activeSessionId: "s2" });
      if (change === "identity") {
        useSessionStore.setState({ research: { s1: [{ ...research, id: "new-task" }] }, bgTasks: { s1: [{ ...background, id: "new-task" }] },
          delegations: { s1: [{ ...delegation, work_id: "new-task" }] }, messages: { s1: [request, receipt("new-task")] } });
        view.rerender(card(kind, "new-task"));
      }
      if (change === "unmount") view.unmount();
      reply(Response.json({ detail: "Old control error" }, { status: 409 }));
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });
}
