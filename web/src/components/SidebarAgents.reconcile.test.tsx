import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SidebarAgents } from "./SidebarAgents";
import { useSessionStore, type Agent } from "../stores/sessionStore";
import { handleWsMessage } from "../hooks/useWebSocket";

const agent = (id: string, name: string): Agent => ({ id, name, backend: "codex", pinned: true, archived: false, is_system: false } as Agent);
beforeEach(() => useSessionStore.setState({ ...useSessionStore.getInitialState(), token: "synthetic-token", accountGeneration: 1, dispatchGeneration: "service-a", agents: [agent("a", "Old A"), agent("b", "Old B")] }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

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
