import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SidebarAgents } from "./SidebarAgents";
import { useSessionStore, type Agent, type SessionInfo } from "../stores/sessionStore";
import { handleWsMessage } from "../hooks/useWebSocket";

const agent = (id: string, name: string): Agent => ({ id, name, backend: "codex", pinned: true, archived: false, is_system: false } as Agent);
beforeEach(() => useSessionStore.setState({ ...useSessionStore.getInitialState(), token: "synthetic-token", accountGeneration: 1, dispatchGeneration: "service-a", agents: [agent("a", "Old A"), agent("b", "Old B")] }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("immediately acknowledges a pending creation and submits it only once", async () => {
  let finish!: (response: Response) => void;
  const fetcher = vi.fn((url: string, init?: RequestInit) => {
    if (init?.method === "POST") return new Promise<Response>((resolve) => { finish = resolve; });
    if (url.endsWith("/api/agents")) return Promise.resolve(Response.json([agent("a", "Old A")]));
    if (url.endsWith("/api/backends")) return Promise.resolve(Response.json({ available: ["codex"] }));
    return Promise.resolve(Response.json([]));
  });
  vi.stubGlobal("fetch", fetcher);
  render(<SidebarAgents />);
  fireEvent.click(await screen.findByLabelText("New session for Old A"));
  const name = screen.getByPlaceholderText("Session name");
  fireEvent.change(name, { target: { value: "New chat" } });
  const create = screen.getByRole("button", { name: "Create session" });
  fireEvent.click(create);
  try {
    expect(screen.getByText("Creating session…")).toHaveAttribute("role", "status");
    expect(create).toBeDisabled();
    fireEvent.click(create);
    fireEvent.keyDown(name, { key: "Enter" });
    expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  } finally {
    await act(async () => finish(new Response(JSON.stringify({ detail: "Unavailable" }), { status: 503 })));
  }
  expect(screen.getByRole("alert")).toHaveTextContent("Unavailable");
  expect(create).toBeEnabled();
});

it("reconciles an uncertain creation without offering an immediate duplicate submission", async () => {
  let fail!: (error: Error) => void;
  const fetcher = vi.fn((url: string, init?: RequestInit) => {
    if (init?.method === "POST") return new Promise<Response>((_resolve, reject) => { fail = reject; });
    if (url.endsWith("/api/agents")) return Promise.resolve(Response.json([agent("a", "Old A")]));
    if (url.endsWith("/api/backends")) return Promise.resolve(Response.json({ available: ["codex"] }));
    return Promise.resolve(Response.json([]));
  });
  vi.stubGlobal("fetch", fetcher);
  render(<SidebarAgents />);
  fireEvent.click(await screen.findByLabelText("New session for Old A"));
  fireEvent.click(screen.getByRole("button", { name: "Create session" }));
  const before = fetcher.mock.calls.filter(([url]) => url.endsWith("/api/sessions")).length;
  await act(async () => fail(new Error("timeout")));
  expect(screen.getByRole("alert")).toHaveTextContent("Creation could not be confirmed");
  expect(screen.queryByRole("button", { name: "Create session" })).toBeNull();
  await waitFor(() => expect(fetcher.mock.calls.filter(([url]) => url.endsWith("/api/sessions")).length).toBeGreaterThan(before));
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it("merges offline additions while preserving updates and archive events newer than the GET", async () => {
  let finish!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (url.endsWith("/api/agents")) return new Promise<Response>((resolve) => { finish = resolve; });
    if (url.endsWith("/api/backends")) return Promise.resolve(Response.json({ available: ["codex"] }));
    return Promise.resolve(Response.json([]));
  }));
  render(<SidebarAgents />);
  await waitFor(() => expect(finish).toBeTypeOf("function"));
  act(() => {
    handleWsMessage({ type: "agent_archived", agent_id: "a" });
    handleWsMessage({ type: "agent_updated", agent: agent("b", "Live B") });
  });
  await act(async () => finish(Response.json([agent("a", "Old A"), agent("b", "Old B"), agent("c", "Offline addition")])));
  expect(screen.queryByText("Old A")).toBeNull();
  expect(screen.queryByText("Old B")).toBeNull();
  expect(screen.getByText("Live B")).toBeVisible();
  expect(screen.getByText("Offline addition")).toBeVisible();
});

it("a late pin receipt cannot revive an agent archived during the request", async () => {
  let finish!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (url.endsWith("/a/unpin")) return new Promise<Response>((resolve) => { finish = resolve; });
    if (url.endsWith("/api/agents")) return Promise.resolve(Response.json([agent("a", "Old A")]));
    return Promise.resolve(Response.json([]));
  }));
  render(<SidebarAgents />);
  fireEvent.click(await screen.findByLabelText("Unpin Old A from the sidebar"));
  await waitFor(() => expect(finish).toBeTypeOf("function"));
  act(() => handleWsMessage({ type: "agent_archived", agent_id: "a" }));
  await act(async () => finish(Response.json({ ...agent("a", "Old A"), pinned: false })));
  expect(useSessionStore.getState().agents.some((row) => row.id === "a")).toBe(false);
  expect(screen.queryByText("Old A")).toBeNull();
});

it.each(["pending", "failed"] as const)("closes a confirmed creation form while its list confirmation is %s without submitting twice", async (confirmation) => {
  const old = { id: "old", name: "Old chat", agent_id: "a", status: "idle", archived: false } as SessionInfo;
  const created = { ...old, id: "new", name: "New chat", status: "idle" } as SessionInfo;
  let finish!: (response: Response) => void;
  let finishConfirmation: ((response: Response) => void) | undefined;
  let saved = false;
  const fetcher = vi.fn((url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    if (init?.method === "POST") return new Promise<Response>((resolve) => { finish = resolve; });
    if (path.endsWith("/api/agents")) return Promise.resolve(Response.json([agent("a", "Old A")]));
    if (path.endsWith("/api/sessions")) {
      if (!saved) return Promise.resolve(Response.json([old]));
      return confirmation === "failed" ? Promise.resolve(new Response(null, { status: 503 })) :
        new Promise<Response>((resolve) => { finishConfirmation = resolve; });
    }
    if (path.endsWith("/api/backends")) return Promise.resolve(Response.json({ available: ["codex"] }));
    return Promise.resolve(Response.json([]));
  });
  vi.stubGlobal("fetch", fetcher);
  const view = render(<SidebarAgents />);
  await waitFor(() => expect(useSessionStore.getState().sessions.map((row) => row.id)).toEqual([old.id]));
  fireEvent.click(await screen.findByLabelText("New session for Old A"));
  fireEvent.change(screen.getByPlaceholderText("Session name"), { target: { value: created.name } });
  fireEvent.click(view.container.querySelector<HTMLButtonElement>("button.btn-create")!);
  await waitFor(() => expect(finish).toBeTypeOf("function"));
  act(() => handleWsMessage({ type: "status", session_id: old.id, status: "running" }));
  await act(async () => { saved = true; finish(Response.json(created)); });
  try {
    await waitFor(() => expect(view.container.querySelector(".session-create")).toBeNull());
    expect(screen.queryByRole("button", { name: "Create session" })).toBeNull();
    expect(useSessionStore.getState().activeSessionId).not.toBe(created.id);
    if (confirmation === "failed") expect(screen.getByRole("alert")).toHaveTextContent("Sessions not synchronized");
    else expect(useSessionStore.getState().sessionRead).toMatchObject({ loading: true, stale: true });
    expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  } finally {
    if (finishConfirmation) await act(async () => finishConfirmation!(Response.json([old, created])));
  }
  if (confirmation === "pending") await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe(created.id));
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});
