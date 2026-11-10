import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SidebarAgents } from "./SidebarAgents";
import { useSessionStore, type Agent, type SessionInfo } from "../stores/sessionStore";

const agent = { id: "ag1", name: "Octo", backend: "codex", is_system: true, pinned: true, archived: false } as Agent;
const session = { id: "s1", name: "ideas", status: "running", agent_id: "ag1", backend: "codex", origin: "user", archived: false } as SessionInfo;
const dispatch = { generation: "service", revision: 1, paused: false, items: [], active_request_id: null, active_operation: null };

beforeEach(() => {
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useSessionStore.getState().setToken("tok");
  useSessionStore.setState({ agents: [agent], sessions: [session],
    activeAgentId: "ag1", activeSessionId: "s1", dispatchGeneration: "service" });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function mockServer(post: () => Promise<Response>, archived = false) {
  const fetcher = vi.fn(async (url: RequestInfo | URL) => {
    const path = new URL(String(url), window.location.origin).pathname;
    if (path.includes("/archive")) return post();
    if (path.endsWith("/api/agents")) return Response.json([agent]);
    if (path.endsWith("/api/sessions")) return Response.json([session]);
    if (path.endsWith("/api/sessions/s1")) return Response.json({ ...session, archived, messages: [],
      dispatch_state: archived ? null : dispatch, active_attempt: null, pending_questions: [], subagents: [] });
    if (path.endsWith("/api/backends")) return Response.json({ available: ["codex"] });
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}

async function archive() {
  render(<SidebarAgents />);
  await act(async () => {});
  fireEvent.click(screen.getByText("Octo"));
  fireEvent.click(await screen.findByLabelText("Archive ideas"));
}

it.each([404, 409, 503])("keeps the live conversation and selection after HTTP %s refuses archival", async (status) => {
  const fetcher = mockServer(async () => Response.json({ detail: "Archive refused" }, { status }));
  await archive();
  expect((await screen.findByRole("alert")).textContent).toBe("Archive refused");
  const state = useSessionStore.getState();
  expect(state.sessions).toEqual([session]);
  expect(state.archivedSessions).toEqual([]);
  expect(state.activeSessionId).toBe("s1");
  expect(fetcher.mock.calls.some(([url]) => new URL(String(url), window.location.origin).pathname.endsWith("/api/sessions/s1"))).toBe(false);
});

it("confirms a lost POST response and preserves the still-live conversation", async () => {
  const fetcher = mockServer(async () => { throw new TypeError("Network interrupted"); });
  await archive();
  expect((await screen.findByRole("alert")).textContent).toBe("Network interrupted");
  const state = useSessionStore.getState();
  expect(state.sessions).toEqual([session]);
  expect(state.archivedSessions).toEqual([]);
  expect(state.activeSessionId).toBe("s1");
  expect(fetcher.mock.calls.filter(([url]) => new URL(String(url), window.location.origin).pathname.endsWith("/api/sessions/s1"))).toHaveLength(1);
});

it("adopts an authoritative archived detail after the POST response is lost", async () => {
  mockServer(async () => { throw new TypeError("Network interrupted"); }, true);
  await archive();
  await waitFor(() => expect(useSessionStore.getState().sessions).toEqual([]));
  expect(useSessionStore.getState().archivedSessions).toEqual([expect.objectContaining({ id: "s1", archived: true })]);
  expect(useSessionStore.getState().activeSessionId).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("confirms network-lost archival before any session establishes a dispatch generation", async () => {
  useSessionStore.setState({ activeSessionId: null, dispatchGeneration: null, dispatchStates: {} });
  mockServer(async () => { throw new TypeError("Network interrupted"); }, true);
  await archive();
  await waitFor(() => expect(useSessionStore.getState().sessions).toEqual([]));
  expect(useSessionStore.getState().archivedSessions).toEqual([expect.objectContaining({ id: "s1", archived: true })]);
  expect(useSessionStore.getState().activeSessionId).toBeNull();
  // Archived detail has no live dispatch_state; it must not invent a service
  // generation merely to confirm the archive operation.
  expect(useSessionStore.getState().dispatchGeneration).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("archives after a successful 204 without duplicating a preceding server event", async () => {
  mockServer(async () => {
    const store = useSessionStore.getState();
    store.setSessions([]);
    store.setArchivedSessions([{ ...session, archived: true }]);
    return new Response(null, { status: 204 });
  });
  await archive();
  await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBeNull());
  expect(useSessionStore.getState().sessions).toEqual([]);
  expect(useSessionStore.getState().archivedSessions).toEqual([{ ...session, archived: true }]);
  expect(screen.queryByRole("alert")).toBeNull();
});

it("does not change another account after a late successful response", async () => {
  let finish!: (response: Response) => void;
  mockServer(() => new Promise<Response>((resolve) => { finish = resolve; }));
  await archive();
  await act(async () => {
    useSessionStore.getState().setToken("another-account");
    finish(new Response(null, { status: 204 }));
  });
  expect(useSessionStore.getState().archivedSessions).toEqual([]);
  expect(screen.queryByRole("alert")).toBeNull();
});
