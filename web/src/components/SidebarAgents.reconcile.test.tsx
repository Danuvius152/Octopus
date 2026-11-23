import { backendCatalogFixture } from "../test-support/backendCatalog";
import { deferred } from "../test-support/deferred";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SidebarAgents } from "./SidebarAgents";
import { useSessionStore, type Agent, type SessionInfo } from "../stores/sessionStore";
import { handleWsMessage } from "../hooks/useWebSocket";

const agent = (id: string, name: string): Agent => ({ id, name, backend: "codex", pinned: true, archived: false, is_system: false,
  description: "", system_prompt: "", mcp_servers: [], tool_allow: "", tool_deny: "", subagents: [],
  created_at: "now", updated_at: "now", active_session_count: 0 });
beforeEach(() => useSessionStore.setState({ ...useSessionStore.getInitialState(), token: "synthetic-token", accountGeneration: 1, dispatchGeneration: "service-a", agents: [agent("a", "Old A"), agent("b", "Old B")] }));
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it.each([{ isComposing: true }, { keyCode: 229 }])("keeps the new-session form during IME Enter %j", async flags => {
  let finish!: (response: Response) => void;
  const fetcher = vi.fn((url: string, init?: RequestInit) => {
    if (init?.method === "POST") return new Promise<Response>(resolve => { finish = resolve; });
    if (url.endsWith("/api/agents")) return Promise.resolve(Response.json([agent("a", "Old A")]));
    return Promise.resolve(Response.json([]));
  });
  vi.stubGlobal("fetch", fetcher);
  render(<SidebarAgents />);
  fireEvent.click(await screen.findByLabelText("New session for Old A"));
  const name = screen.getByPlaceholderText("Session name");
  fireEvent.change(name, { target: { value: "中文会话" } });
  const event = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true, ...flags });
  fireEvent(name, event);
  expect(event.defaultPrevented).toBe(false);
  expect(name).toHaveValue("中文会话");
  expect(name).toBeEnabled();
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
  fireEvent.keyDown(name, { key: "Enter" });
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  await act(async () => finish(Response.json({ detail: "Controlled refusal" }, { status: 503 })));
});

it("immediately acknowledges a pending creation and submits it only once", async () => {
  let finish!: (response: Response) => void;
  const fetcher = vi.fn((url: string, init?: RequestInit) => {
    if (init?.method === "POST") return new Promise<Response>((resolve) => { finish = resolve; });
    if (url.endsWith("/api/agents")) return Promise.resolve(Response.json([agent("a", "Old A")]));
    if (url.endsWith("/api/backends")) return Promise.resolve(Response.json(backendCatalogFixture(["codex"])));
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
    await act(async () => finish(new Response(JSON.stringify({ detail: "Invalid session name" }), { status: 400 })));
  }
  expect(screen.getByRole("alert")).toHaveTextContent("Invalid session name");
  expect(create).toBeEnabled();
});

it("reconciles an uncertain creation without offering an immediate duplicate submission", async () => {
  let fail!: (error: Error) => void;
  const fetcher = vi.fn((url: string, init?: RequestInit) => {
    if (init?.method === "POST") return new Promise<Response>((_resolve, reject) => { fail = reject; });
    if (url.endsWith("/api/agents")) return Promise.resolve(Response.json([agent("a", "Old A")]));
    if (url.endsWith("/api/backends")) return Promise.resolve(Response.json(backendCatalogFixture(["codex"])));
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
    if (url.endsWith("/api/backends")) return Promise.resolve(Response.json(backendCatalogFixture(["codex"])));
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

it.each(["pending", "failed"] as const)("keeps the original form while list confirmation is %s", async (confirmation) => {
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
    if (path.endsWith("/api/backends")) return Promise.resolve(Response.json(backendCatalogFixture(["codex"])));
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
  await act(async () => { saved = true; finish(Response.json(created, { status: 201 })); });
  try {
    expect(screen.getByRole("group", { name: "New session" })).toHaveAttribute("aria-busy", String(confirmation === "pending"));
    expect(screen.getByRole("button", { name: "Create session" })).toBeDisabled();
    expect(useSessionStore.getState().activeSessionId).not.toBe(created.id);
    if (confirmation === "failed") expect(screen.getByText("Session created (new), but the list could not be confirmed.")).toBeVisible();
    else expect(useSessionStore.getState().sessionRead).toMatchObject({ loading: true, stale: true });
    expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  } finally {
    if (finishConfirmation) await act(async () => finishConfirmation!(Response.json([old, created])));
  }
  if (confirmation === "pending") await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe(created.id));
  if (confirmation === "pending") expect(screen.queryByRole("group", { name: "New session" })).toBeNull();
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

const original = { id: "original", name: "Original chat", agent_id: "a", status: "idle", archived: false } as SessionInfo;
const created = { ...original, id: "created-id", name: "Created chat" };

async function startSidebarCreation(fakeTimers = false) {
  const posts: ReturnType<typeof deferred<Response>>[] = [];
  const reads: (ReturnType<typeof deferred<Response>> & { signal: AbortSignal })[] = [];
  let holdReads = false;
  let rows = [original];
  const fetcher = vi.fn((url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    if (init?.method === "POST") {
      const reply = deferred<Response>();
      posts.push(reply);
      return reply.promise;
    }
    if (path === "/api/sessions") {
      if (!holdReads) return Promise.resolve(Response.json(rows));
      const read = { ...deferred<Response>(), signal: init!.signal! };
      reads.push(read);
      return read.promise;
    }
    if (path === "/api/agents") return Promise.resolve(Response.json([agent("a", "Old A")]));
    if (path === "/api/backends") return Promise.resolve(Response.json(backendCatalogFixture(["codex"])));
    return Promise.resolve(Response.json([]));
  });
  vi.stubGlobal("fetch", fetcher);
  useSessionStore.getState().setActiveSessionId(original.id);
  const view = render(<SidebarAgents />);
  await waitFor(() => expect(useSessionStore.getState().sessions).toEqual([original]));
  fireEvent.click(await screen.findByRole("button", { name: "New session for Old A" }));
  if (fakeTimers) vi.useFakeTimers();
  fireEvent.click(screen.getByRole("button", { name: "Create session" }));
  return { posts, reads, view, holdReads: () => { holdReads = true; }, commitCreated: () => { rows = [original, created]; } };
}

function statusChanged(status: "idle" | "running") {
  handleWsMessage({ type: "status", session_id: original.id, status });
}

it("ends confirmation at 30 seconds while the shared list can still observe the created session", async () => {
  const { posts, reads, holdReads } = await startSidebarCreation(true);
  holdReads();
  act(() => statusChanged("running"));
  await act(async () => posts[0].resolve(Response.json(created, { status: 201 })));
  expect(reads).toHaveLength(1);
  for (const [i, status] of (["idle", "running"] as const).entries()) {
    await act(async () => vi.advanceTimersByTimeAsync(11_000));
    await act(async () => {
      statusChanged(status);
      reads[i].resolve(Response.json([original, created]));
    });
    expect(reads).toHaveLength(i + 2);
    expect(useSessionStore.getState().sessions.some((row) => row.id === created.id)).toBe(false);
  }
  const form = screen.getByRole("group", { name: "New session" });
  await act(async () => vi.advanceTimersByTimeAsync(7_999));
  expect(form).toHaveAttribute("aria-busy", "true");
  await act(async () => vi.advanceTimersByTimeAsync(1));
  expect(form).toHaveAttribute("aria-busy", "false");
  expect(screen.getByRole("alert")).toHaveTextContent("Session created (created-id), but the list could not be confirmed.");
  expect(screen.getByRole("button", { name: "Create session" })).toBeDisabled();
  expect(useSessionStore.getState().activeSessionId).toBe(original.id);
  expect(reads[2].signal.aborted).toBe(false);

  fireEvent.click(screen.getByRole("button", { name: "Check session list" }));
  expect(reads).toHaveLength(3);
  await act(async () => reads[2].resolve(Response.json([original, created])));
  expect(screen.getByText(created.name)).toBeVisible();
  expect(useSessionStore.getState().activeSessionId).toBe(original.id);
  expect(form).toBeVisible();
  expect(posts).toHaveLength(1);
});

it.each(["archive", "delete"] as const)("keeps the form when a %s observation overtakes the creation confirmation read", async (effect) => {
  const { posts, reads, holdReads } = await startSidebarCreation();
  holdReads();
  act(() => statusChanged("running"));
  await act(async () => posts[0].resolve(Response.json(created, { status: 201 })));
  expect(reads).toHaveLength(1);
  act(() => handleWsMessage(effect === "archive"
    ? { type: "session_archived", old_session_id: created.id, new_session_id: null }
    : { type: "session_deleted", session_id: created.id }));
  await act(async () => reads[0].resolve(Response.json([original, created])));
  expect(reads).toHaveLength(2);
  expect(screen.getByRole("group", { name: "New session" })).toHaveAttribute("aria-busy", "false");
  expect(useSessionStore.getState().activeSessionId).toBe(original.id);
  await act(async () => reads[1].resolve(Response.json(effect === "archive" ? [original, { ...created, archived: true }] : [original])));
  expect(useSessionStore.getState().sessions).toEqual([original]);
  expect(useSessionStore.getState().archivedSessions).toEqual(effect === "archive" ? [{ ...created, archived: true }] : []);
  expect(useSessionStore.getState().activeSessionId).toBe(original.id);
  expect(screen.queryByText(created.name)).toBeNull();
  expect(posts).toHaveLength(1);
});

it.each([
  ["navigate", "created"], ["navigate", "refused"],
  ["reopen", "created"], ["reopen", "refused"],
  ["account", "created"], ["account", "refused"],
  ["unmount", "created"], ["unmount", "refused"],
] as const)("keeps the new form after %s when the old POST returns %s", async (leave, result) => {
  const { posts, view, commitCreated } = await startSidebarCreation();
  if (leave === "reopen") fireEvent.click(screen.getByRole("button", { name: "Cancel new session" }));
  if (leave === "navigate") act(() => {
    const { setActiveSessionId } = useSessionStore.getState();
    setActiveSessionId("other");
    setActiveSessionId(original.id);
  });
  if (leave === "account") act(() => useSessionStore.getState().setToken("next-account"));
  if (leave === "unmount") {
    view.unmount();
    render(<SidebarAgents />);
  }
  fireEvent.click(await screen.findByRole("button", { name: "New session for Old A" }));
  const name = screen.getByPlaceholderText("Session name");
  fireEvent.change(name, { target: { value: "Independent draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Create session" }));
  expect(posts).toHaveLength(2);
  if (result === "created" && leave !== "account") commitCreated();
  await act(async () => posts[0].resolve(result === "created"
    ? Response.json(created, { status: 201 })
    : Response.json({ detail: "Failure for the old form" }, { status: 400 })));
  expect(screen.getByPlaceholderText("Session name")).toHaveValue("Independent draft");
  expect(screen.getByRole("group", { name: "New session" })).toHaveAttribute("aria-busy", "true");
  expect(screen.queryByRole("alert")).toBeNull();
  expect(useSessionStore.getState().activeSessionId).not.toBe(created.id);
  expect(useSessionStore.getState().sessions.some((row) => row.id === created.id)).toBe(result === "created" && leave !== "account");
  await act(async () => posts[1].resolve(Response.json({ detail: "Failure for the current form" }, { status: 400 })));
  expect(screen.getByRole("alert")).toHaveTextContent("Failure for the current form");
  expect(posts).toHaveLength(2);
});

it.each(["headers", "body"] as const)("ends an unanswered POST waiting for %s within its original deadline", async (waiting) => {
  const { posts, reads, holdReads } = await startSidebarCreation(true);
  holdReads();
  let body: ReadableStreamDefaultController<Uint8Array> | undefined;
  if (waiting === "body") {
    await act(async () => vi.advanceTimersByTimeAsync(22_000));
    const stream = new ReadableStream<Uint8Array>({ start: (controller) => { body = controller; } });
    await act(async () => posts[0].resolve(new Response(stream, { status: 201 })));
  }
  await act(async () => vi.advanceTimersByTimeAsync(waiting === "body" ? 7_999 : 29_999));
  expect(screen.getByRole("group", { name: "New session" })).toHaveAttribute("aria-busy", "true");
  await act(async () => vi.advanceTimersByTimeAsync(1));
  expect(screen.queryByRole("group", { name: "New session" })).toBeNull();
  expect(screen.getByRole("alert")).toHaveTextContent("Check the session list before creating another conversation.");
  expect(posts).toHaveLength(1);
  expect(reads).toHaveLength(1);
  await act(async () => {
    if (body) body.close();
    else posts[0].resolve(Response.json(created, { status: 201 }));
    reads[0].resolve(Response.json([original]));
  });
  expect(useSessionStore.getState().activeSessionId).toBe(original.id);
  expect(posts).toHaveLength(1);
});
