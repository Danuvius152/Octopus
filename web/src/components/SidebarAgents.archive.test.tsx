import { backendCatalogFixture } from "../test-support/backendCatalog";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SidebarAgents } from "./SidebarAgents";
import { sessions as sessionList } from "../lib/sessionList";
import { useSessionStore, type Agent, type SessionInfo } from "../stores/sessionStore";
import { deferred } from "../test-support/deferred";

const agent = { id: "ag1", name: "Octo", backend: "codex", is_system: true, pinned: true, archived: false } as Agent;
const session = { id: "s1", name: "ideas", status: "running", agent_id: "ag1", backend: "codex", origin: "user", archived: false } as SessionInfo;
const dispatch = { generation: "service", revision: 1, paused: false, items: [], active_request_id: null, active_operation: null };

beforeEach(() => {
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useSessionStore.getState().setToken("tok");
  useSessionStore.setState({ agents: [agent], sessions: [session],
    activeAgentId: "ag1", activeSessionId: "s1", dispatchGeneration: "service" });
});
afterEach(() => { cleanup(); useSessionStore.getState().setToken(""); vi.useRealTimers(); vi.unstubAllGlobals(); });

function mockServer(post: () => Promise<Response>, archived = false, confirm?: () => Promise<Response>) {
  let posted = false;
  const fetcher = vi.fn(async (url: RequestInfo | URL) => {
    const parsed = new URL(String(url), window.location.origin), path = parsed.pathname;
    if (path.includes("/archive")) { posted = true; return post(); }
    if (path.endsWith("/api/agents")) return Response.json([agent]);
    if (path.endsWith("/api/sessions")) return posted && parsed.searchParams.has("include_archived") && confirm ? confirm() :
      Response.json([{ ...session, archived: posted && archived }]);
    if (path.endsWith("/api/sessions/s1")) return Response.json({ ...session, archived, messages: [],
      dispatch_state: archived ? null : dispatch, active_attempt: null, pending_questions: [], subagents: [] });
    if (path.endsWith("/api/backends")) return Response.json(backendCatalogFixture(["codex"]));
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}

async function archive() {
  render(<SidebarAgents />);
  await act(async () => {});
  fireEvent.click(screen.getByText("Octo"));
  fireEvent.click(screen.getByLabelText("Archive ideas"));
  await act(async () => {});
}

it.each([404, 409])("keeps the live conversation and selection after HTTP %s refuses archival", async (status) => {
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
  expect(await screen.findByRole("alert")).toHaveTextContent("Archive result is still unknown");
  expect(screen.getByRole("button", { name: "Confirm original archive" })).toBeEnabled();
  const state = useSessionStore.getState();
  expect(state.sessions).toEqual([session]);
  expect(state.archivedSessions).toEqual([]);
  expect(state.activeSessionId).toBe("s1");
  expect(fetcher.mock.calls.filter(([url]) => new URL(String(url), window.location.origin).searchParams.has("include_archived"))).toHaveLength(1);
});

it.each([false, true])("reopens A's original unknown archive after B refuses, overlapping confirmation=%s", async (overlap) => {
  const other = { ...session, id: "s2", name: "other" };
  const firstRead = deferred<Response>();
  let confirmed = false, reads = 0;
  const posts: string[] = [];
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), window.location.origin);
    if (init?.method === "POST") {
      posts.push(url.pathname);
      if (url.pathname.includes("/s1/")) throw new TypeError("Lost archive reply");
      return Response.json({ detail: "B refused" }, { status: 409 });
    }
    if (url.pathname === "/api/agents") return Response.json([agent]);
    if (url.pathname === "/api/sessions") {
      if (url.searchParams.has("include_archived") && ++reads === 1 && overlap) return firstRead.promise;
      return Response.json([{ ...session, archived: confirmed }, other].filter(row => url.searchParams.has("include_archived") || !row.archived));
    }
    if (url.pathname === "/api/backends") return Response.json(backendCatalogFixture(["codex"]));
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  await archive();
  if (!overlap) expect(screen.getByRole("alert")).toHaveTextContent("Archive result is still unknown");
  await act(async () => fireEvent.click(screen.getByLabelText("Archive other")));
  expect(screen.getByRole("alert")).toHaveTextContent("B refused");
  if (overlap) await act(async () => firstRead.resolve(Response.json([session, other])));
  fireEvent.click(screen.getByLabelText("Archive ideas"));
  expect(screen.getByRole("alert")).toHaveTextContent("Archive result is still unknown");
  expect(screen.getByRole("button", { name: "Confirm original archive" })).toBeEnabled();
  confirmed = true;
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Confirm original archive" })));
  expect(useSessionStore.getState().archivedSessions.map(row => row.id)).toContain(session.id);
  expect(posts).toEqual(["/api/sessions/s1/archive", "/api/sessions/s2/archive"]);
});

it("adopts an authoritative inclusive list after the POST response is lost", async () => {
  mockServer(async () => { throw new TypeError("Network interrupted"); }, true);
  await archive();
  await waitFor(() => expect(useSessionStore.getState().sessions).toEqual([]));
  expect(useSessionStore.getState().archivedSessions).toEqual([expect.objectContaining({ id: "s1", archived: true })]);
  expect(useSessionStore.getState().activeSessionId).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("treats a generic server failure as unknown and never calls it a proven refusal", async () => {
  const fetcher = mockServer(async () => Response.json({ detail: "Server unavailable" }, { status: 503 }));
  await archive();
  expect(await screen.findByRole("alert")).toHaveTextContent("Archive result is still unknown");
  expect(screen.queryByText("Server unavailable")).toBeNull();
  expect(fetcher.mock.calls.filter(([url]) => String(url).includes("/archive?"))).toHaveLength(1);
});

it.each(["headers", "success body", "error body"])("bounds sidebar archive %s and confirms manually without a second POST", async (stage) => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  let blocked = true;
  const fetcher = mockServer(async () => {
    if (stage === "headers") return new Promise(() => {});
    const response = new Response(null, { status: stage === "success body" ? 201 : 500 });
    response.json = () => new Promise(() => {});
    return response;
  }, false, async () => blocked ? new Promise(() => {}) : Response.json([{ ...session, archived: true }]));
  await archive();
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(screen.getByRole("button", { name: "Archive ideas" })).toHaveAttribute("aria-busy", "true");
  expect(screen.queryByRole("alert")).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(screen.getByText(/Archive result is still unknown/)).toBeVisible();
  const button = screen.getByRole("button", { name: "Confirm original archive" });
  expect(button).toBeEnabled();
  blocked = false;
  await act(async () => { fireEvent.click(button); });
  expect(useSessionStore.getState().archivedSessions.map((row) => row.id)).toContain(session.id);
  // A manual confirmation has no authority to restore old navigation.
  expect(useSessionStore.getState().activeSessionId).toBe(session.id);
  expect(fetcher.mock.calls.filter(([url]) => String(url).includes("/archive?"))).toHaveLength(1);
});

it("confirms network-lost archival before any session establishes a dispatch generation", async () => {
  useSessionStore.setState({ activeSessionId: null, dispatchGeneration: null, dispatchStates: {} });
  mockServer(async () => { throw new TypeError("Network interrupted"); }, true);
  await archive();
  await waitFor(() => expect(useSessionStore.getState().sessions).toEqual([]));
  expect(useSessionStore.getState().archivedSessions).toEqual([expect.objectContaining({ id: "s1", archived: true })]);
  expect(useSessionStore.getState().activeSessionId).toBeNull();
  // Inclusive list confirmation must not invent a dispatch generation.
  expect(useSessionStore.getState().dispatchGeneration).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("confirms a successful 204 silently after a preceding archive event", async () => {
  const reply = deferred<Response>();
  const list = deferred<Response>();
  const confirm = vi.fn(() => list.promise);
  const fetcher = mockServer(() => reply.promise, true, confirm);
  await archive();
  await act(async () => {
    sessionList.archive(session.id, undefined, null);
    reply.resolve(new Response(null, { status: 204 }));
  });
  await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
  expect(screen.queryByRole("alert")).toBeNull();

  await act(async () => list.resolve(Response.json([{ ...session, archived: true }])));
  await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBeNull());
  expect(useSessionStore.getState().sessions).toEqual([]);
  expect(useSessionStore.getState().archivedSessions).toEqual([{ ...session, archived: true }]);
  expect(screen.queryByRole("alert")).toBeNull();
  expect(fetcher.mock.calls.filter(([url]) => String(url).includes("/archive?"))).toHaveLength(1);
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
