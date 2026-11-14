import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { mockChatViewport } from "./chatViewport.test-support";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { deferred } from "../test-support/deferred";

const a = { id: "view-a", name: "A", status: "idle", backend: "codex", working_dir: "/tmp", created_at: "now" } as SessionInfo;
const b = { ...a, id: "view-b", name: "B" };
const reads: ReturnType<typeof deferred<Response>>[] = [];
beforeEach(() => {
  mockChatViewport();
  useSessionStore.getState().setToken("");
  useSessionStore.getState().setToken("view-token");
  useSessionStore.setState({ mainView: "chat", activeSessionId: a.id, sessions: [a, b], archivedSessions: [], agents: [], messages: {},
    pendingQuestions: {}, streamingText: {}, dispatchStates: {}, dispatchUnavailable: {}, pendingForks: {}, credentials: [], backendModels: {}, viewer: null });
  reads.length = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    if (String(url).endsWith("/showme/resolve") || init?.method === "PATCH") {
      const reply = deferred<Response>(); reads.push(reply); return reply.promise;
    }
    return Response.json(String(url).endsWith("/submissions") ? { items: [], next_cursor: null } : []);
  }));
});
afterEach(() => { cleanup(); useSessionStore.getState().setToken(""); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
async function mount() {
  let view!: ReturnType<typeof render>;
  await act(async () => { view = render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={vi.fn()} answerQuestion={vi.fn()} connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider>); });
  return view;
}
function command(text: string) {
  fireEvent.change(screen.getByRole("textbox"), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
}

it.each(["success", "failure"])("retires a viewing intent permanently through a same-batch session round trip (%s)", async outcome => {
  await mount();
  command("/showme first report");
  act(() => { useSessionStore.setState({ activeSessionId: b.id }); useSessionStore.setState({ activeSessionId: a.id }); });
  await act(async () => reads[0].resolve(outcome === "success" ? Response.json({ path: "/obsolete/report.md" }) : Response.json({ detail: "Obsolete view failure" }, { status: 400 })));
  expect(useSessionStore.getState().viewer).toBeNull();
  expect(useSessionStore.getState().messages[a.id].some(message => message.is_error)).toBe(false);
});

it.each(["selection", "page", "page round trip", "close viewer", "explicit old file", "account round trip", "unmount"].flatMap(
  transition => ["success", "failure"].map(outcome => ({ transition, outcome })),
))("retires viewing on $transition without adopting late $outcome", async ({ transition, outcome }) => {
  const view = await mount();
  if (transition === "close viewer") act(() => useSessionStore.getState().openViewer(a.id, "/previous.md"));
  command("/showme first report");
  act(() => {
    if (transition === "selection") useSessionStore.setState({ activeSessionId: b.id });
    if (transition.startsWith("page")) {
      useSessionStore.setState({ mainView: "schedules" });
      if (transition === "page round trip") useSessionStore.setState({ mainView: "chat" });
    }
    if (transition === "close viewer") useSessionStore.getState().closeViewer();
    if (transition === "explicit old file") useSessionStore.getState().openViewer(b.id, "/explicit/older.md");
    if (transition === "account round trip") {
      useSessionStore.getState().setToken("another-account");
      useSessionStore.getState().setToken("view-token");
      useSessionStore.setState({ mainView: "chat", activeSessionId: a.id, sessions: [a, b] });
    }
    if (transition === "unmount") view.unmount();
  });
  await act(async () => {
    if (outcome === "success") reads[0].resolve(Response.json({ path: "/obsolete/report.md" }));
    else reads[0].reject(new TypeError("Retired network failure"));
  });
  expect(useSessionStore.getState().viewer).toEqual(transition === "explicit old file" ? { sessionId: b.id, path: "/explicit/older.md" } : null);
  expect(Object.values(useSessionStore.getState().messages).flat().some(message => message.is_error)).toBe(false);
});

it.each(["success", "failure"])("keeps a newer viewing intent through an old %s and cleanup", async outcome => {
  await mount();
  command("/showme first report");
  command("/showme second report");
  await act(async () => reads[0].resolve(outcome === "success" ? Response.json({ path: "/old.md" }) : Response.json({ detail: "Old failure" }, { status: 409 })));
  expect(useSessionStore.getState().viewer).toBeNull();
  await act(async () => reads[1].resolve(Response.json({ path: "/new.md" })));
  expect(useSessionStore.getState().viewer).toEqual({ sessionId: a.id, path: "/new.md" });
  expect(useSessionStore.getState().messages[a.id].some(message => message.is_error)).toBe(false);
});

it("keeps the newer viewer when its response finishes before the old viewing", async () => {
  await mount();
  command("/showme first report");
  command("/showme second report");
  await act(async () => reads[1].resolve(Response.json({ path: "/new.md" })));
  await act(async () => reads[0].resolve(Response.json({ path: "/old.md" })));
  expect(useSessionStore.getState().viewer).toEqual({ sessionId: a.id, path: "/new.md" });
});

it.each([200, 400])("ignores a body delivered after account retirement (HTTP %s)", async status => {
  await mount();
  command("/showme report");
  let body!: ReadableStreamDefaultController<Uint8Array>;
  await act(async () => reads[0].resolve(new Response(new ReadableStream<Uint8Array>({ start(controller) { body = controller; } }), { status })));
  act(() => {
    useSessionStore.getState().setToken("another-account");
    useSessionStore.setState({ activeSessionId: a.id, sessions: [a, b] });
  });
  await act(async () => { body.enqueue(new TextEncoder().encode(JSON.stringify(status === 200 ? { path: "/old.md" } : { detail: "Old error body" }))); body.close(); });
  expect(useSessionStore.getState().viewer).toBeNull();
  expect(useSessionStore.getState().messages[a.id] ?? []).toEqual([]);
});

it.each(["HTTP", "network", "no match"])("shows a current legitimate %s failure", async outcome => {
  await mount();
  command("/showme report");
  await act(async () => {
    if (outcome === "network") reads[0].reject(new TypeError("offline"));
    else reads[0].resolve(Response.json(outcome === "HTTP" ? { detail: "File unavailable" } : { message: "No matching report" }, { status: outcome === "HTTP" ? 400 : 200 }));
  });
  expect(useSessionStore.getState().messages[a.id]).toContainEqual(expect.objectContaining({ is_error: true, content: outcome === "HTTP" ? "File unavailable" : outcome === "network" ? "Couldn't resolve file reference — network error." : "No matching report" }));
});

it.each(["HTTP", "network", "body"])("does not publish a retired model %s error into another account", async phase => {
  await mount();
  command("/model next-model");
  let body!: ReadableStreamDefaultController<Uint8Array>;
  if (phase === "body") await act(async () => reads[0].resolve(new Response(new ReadableStream<Uint8Array>({ start(controller) { body = controller; } }))));
  act(() => {
    useSessionStore.getState().setToken("another-account");
    useSessionStore.setState({ mainView: "chat", sessions: [a, b], activeSessionId: a.id });
  });
  await act(async () => {
    if (phase === "HTTP") reads[0].resolve(Response.json({ detail: "Old model failure" }, { status: 400 }));
    else if (phase === "network") reads[0].reject(new TypeError("offline"));
    else { body.enqueue(new TextEncoder().encode(JSON.stringify({ ...a, model: "old-model" }))); body.close(); }
  });
  expect(useSessionStore.getState().messages[a.id] ?? []).toEqual([]);
  expect(useSessionStore.getState().sessions[0].model).not.toBe("old-model");
});
