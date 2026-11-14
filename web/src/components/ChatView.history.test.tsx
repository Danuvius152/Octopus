import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { mockChatViewport } from "./chatViewport.test-support";
import { useSessionStore, type Message, type SessionInfo } from "../stores/sessionStore";
import { deferred } from "../test-support/deferred";

const session = { id: "history-a", name: "A", status: "idle", backend: "codex", created_at: "2026-11-14", working_dir: "/tmp" } as SessionInfo;
const box = () => screen.getByRole<HTMLTextAreaElement>("textbox");
const send = () => screen.getByRole("button", { name: "Send message" });
const edit = (value: string) => fireEvent.change(box(), { target: { value } });
const arrow = (key: "ArrowUp" | "ArrowDown") => {
  box().setSelectionRange(box().value.length, box().value.length);
  fireEvent.keyDown(box(), { key });
};
const prompt = (seq: number, content: string, preview = false): Message => ({ seq, role: "user", type: "text", content,
  ...(preview ? { body_state: "preview", detail_ref: `/api/sessions/${session.id}/messages/${seq}` } : {}) });
const reads: { seq: number; reply: ReturnType<typeof deferred<Response>>; signal?: AbortSignal | null }[] = [];
let transport: ReturnType<typeof vi.fn<typeof fetch>>;

function mount(messages: Message[]) {
  useSessionStore.setState({ messages: { [session.id]: messages } });
  const sendMessage = vi.fn<Parameters<typeof ChatView>[0]["sendMessage"]>();
  return { ...render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={sendMessage} interrupt={vi.fn()} answerQuestion={vi.fn()} connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider>), sendMessage };
}
beforeEach(() => {
  mockChatViewport();
  useSessionStore.getState().setToken("");
  sessionStorage.clear();
  useSessionStore.getState().setToken("history-test");
  useSessionStore.setState({ activeSessionId: session.id, sessions: [session], archivedSessions: [], mainView: "chat",
    agents: [], messages: {}, streamingText: {}, pendingQuestions: {}, dispatchStates: {}, dispatchUnavailable: {},
    pendingForks: {}, credentials: [], backendModels: {} });
  reads.length = 0;
  transport = vi.fn((url: RequestInfo | URL, init?: RequestInit) => {
    const match = String(url).match(/\/messages\/(\d+)\?view=browser$/);
    if (!match) return Promise.resolve(Response.json(String(url).endsWith("/submissions") ? { items: [], next_cursor: null } : []));
    const reply = deferred<Response>();
    reads.push({ seq: Number(match[1]), reply, signal: init?.signal });
    return reply.promise;
  });
  vi.stubGlobal("fetch", transport);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); useSessionStore.getState().setToken(""); });

it.each(["response", "body"])("ends a full-message timeout during the %s without replacing the draft", async phase => {
  vi.useFakeTimers();
  // Advance the platform deadline without spending 30 seconds in this test.
  vi.spyOn(AbortSignal, "timeout").mockImplementation(delay => {
    const deadline = new AbortController();
    setTimeout(() => deadline.abort(new DOMException("History read timed out", "TimeoutError")), delay);
    return deadline.signal;
  });
  mount([prompt(1, "Preview…", true)]);
  edit("Original draft");
  arrow("ArrowUp");
  let body!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start(controller) { body = controller; } });
  reads[0].signal!.addEventListener("abort", () => {
    if (phase === "response") reads[0].reply.reject(reads[0].signal!.reason);
    else body.error(reads[0].signal!.reason);
  });
  if (phase === "body") await act(async () => reads[0].reply.resolve(new Response(stream)));
  expect(send()).toBeDisabled();
  await act(async () => { await vi.advanceTimersByTimeAsync(30_001); });
  expect(screen.getByRole("alert")).toHaveTextContent("Could not load full history message.");
  expect(send()).toBeEnabled();
  expect(box()).toHaveValue("Original draft");
  arrow("ArrowDown");
  expect(box()).toHaveValue("Original draft");
});

it("ignores a complete body delivered after a newer edit while body reading was pending", async () => {
  mount([prompt(1, "Preview…", true)]);
  edit("Original draft");
  arrow("ArrowUp");
  let body!: ReadableStreamDefaultController<Uint8Array>;
  await act(async () => reads[0].reply.resolve(new Response(new ReadableStream<Uint8Array>({ start(controller) { body = controller; } }))));
  expect(send()).toBeDisabled();
  edit("New draft 中文");
  expect(send()).toBeEnabled();
  await act(async () => {
    body.enqueue(new TextEncoder().encode(JSON.stringify(prompt(1, "Obsolete full body"))));
    body.close();
  });
  arrow("ArrowDown");
  expect(box()).toHaveValue("New draft 中文");
});

it("recalls only a selected preview's full text and sends its tail without expanding the transcript", async () => {
  const row = prompt(2, "A shared preview…", true);
  const { sendMessage } = mount([prompt(1, "Inline complete"), row]);
  edit("Keep my unsent draft");
  expect(reads).toHaveLength(0);
  arrow("ArrowUp");
  expect(box()).toHaveValue("Keep my unsent draft");
  expect(screen.getByText("Loading full history message…")).toHaveAttribute("role", "status");
  expect(send()).toBeDisabled();
  fireEvent.click(send());
  fireEvent.keyDown(box(), { key: "Enter" });
  expect(sendMessage).not.toHaveBeenCalled();
  expect(reads.map(read => read.seq)).toEqual([2]);
  await act(async () => reads[0].reply.resolve(Response.json(prompt(2, "A shared preview and the essential tail"))));
  expect(box()).toHaveValue("A shared preview and the essential tail");
  expect(send()).toBeEnabled();
  expect(useSessionStore.getState().messages[session.id][1]).toEqual(row);
  fireEvent.click(send());
  expect(sendMessage).toHaveBeenCalledExactlyOnceWith(session.id, "A shared preview and the essential tail", undefined, undefined);
});

it("keeps equal previews as separate messages and recalls their distinct tails in order", async () => {
  mount([prompt(1, "Same preview…", true), prompt(2, "Same preview…", true)]);
  edit("Original");
  arrow("ArrowUp");
  await act(async () => reads[0].reply.resolve(Response.json(prompt(2, "Same preview with second tail"))));
  expect(box()).toHaveValue("Same preview with second tail");
  arrow("ArrowUp");
  expect(reads.map(read => read.seq)).toEqual([2, 1]);
  await act(async () => reads[1].reply.resolve(Response.json(prompt(1, "Same preview with first tail"))));
  expect(box()).toHaveValue("Same preview with first tail");
});

it.each(["success", "failure"])("preserves identity-restored text through a late %s and Down", async outcome => {
  sessionStorage.setItem('octopus.text-draft.v1:["history-owner","chat","shared"]', "Restored original 中文");
  mount([prompt(1, "Preview…", true)]);
  arrow("ArrowUp");
  expect(reads).toHaveLength(1);
  act(() => useSessionStore.getState().setIdentity({ user_id: "history-owner", label: "Owner", is_admin: false }));
  expect(box()).toHaveValue("Restored original 中文");
  expect(send()).toBeEnabled();
  expect(reads[0].signal?.aborted).toBe(true);
  await act(async () => {
    if (outcome === "success") reads[0].reply.resolve(Response.json(prompt(1, "Obsolete full message")));
    else reads[0].reply.reject(new Error("Obsolete failure"));
  });
  arrow("ArrowDown");
  expect(box()).toHaveValue("Restored original 中文");
  expect(screen.queryByText("Obsolete failure")).toBeNull();
  expect(screen.queryByText("Loading full history message…")).toBeNull();
});

it.each(["new edit", "Down", "session round trip", "removed candidate", "unmount"])("retires the selected read on %s", async action => {
  const row = prompt(1, "Preview…", true);
  const view = mount([row]);
  edit("Original");
  arrow("ArrowUp");
  if (action === "new edit") edit("Newer edit");
  else if (action === "Down") arrow("ArrowDown");
  else if (action === "session round trip") act(() => {
    useSessionStore.setState({ activeSessionId: "history-b" });
    useSessionStore.setState({ activeSessionId: session.id });
  });
  else if (action === "removed candidate") act(() => {
    useSessionStore.setState({ messages: { [session.id]: [] } });
    useSessionStore.setState({ messages: { [session.id]: [row] } });
  });
  else { view.unmount(); mount([row]); }
  expect(reads[0].signal?.aborted).toBe(true);
  expect(send()).toBeEnabled();
  await act(async () => reads[0].reply.resolve(Response.json(prompt(1, "Obsolete full message"))));
  arrow("ArrowDown");
  expect(box()).toHaveValue(action === "new edit" ? "Newer edit" : "Original");
});

it("keeps a successor blocked when an older read fails, then switches directly to inline history", async () => {
  mount([prompt(1, "Complete inline"), prompt(2, "Older preview…", true), prompt(3, "Newest preview…", true)]);
  edit("Original");
  arrow("ArrowUp");
  arrow("ArrowUp");
  expect(reads.map(read => read.seq)).toEqual([3, 2]);
  await act(async () => reads[0].reply.reject(new Error("Older failure")));
  expect(send()).toBeDisabled();
  expect(screen.queryByText("Older failure")).toBeNull();
  arrow("ArrowUp");
  expect(box()).toHaveValue("Complete inline");
  expect(send()).toBeEnabled();
  expect(reads).toHaveLength(2);
  await act(async () => reads[1].reply.resolve(Response.json(prompt(2, "Retired older full text"))));
  expect(box()).toHaveValue("Complete inline");
});

it("preserves an edit back to identical text and retires history through account round trips", async () => {
  const row = prompt(1, "Preview…", true);
  mount([row]);
  edit("Original");
  arrow("ArrowUp");
  edit("Changed");
  edit("Original");
  await act(async () => reads[0].reply.resolve(Response.json(prompt(1, "Obsolete full text"))));
  expect(box()).toHaveValue("Original");
  arrow("ArrowUp");
  act(() => {
    useSessionStore.getState().setToken("different-account");
    useSessionStore.getState().setToken("history-test");
    useSessionStore.setState({ sessions: [session], activeSessionId: session.id, messages: { [session.id]: [row] } });
  });
  edit("New account draft");
  await act(async () => reads[1].reply.resolve(Response.json(prompt(1, "Old account full text"))));
  arrow("ArrowDown");
  expect(box()).toHaveValue("New account draft");
  expect(send()).toBeEnabled();
});

it("refuses an unrelated full-message reference without requesting it", async () => {
  mount([{ ...prompt(1, "Preview…", true), detail_ref: "/api/sessions/unrelated/messages/1" }]);
  edit("Original");
  arrow("ArrowUp");
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("No complete saved history message reference"));
  expect(reads).toHaveLength(0);
  expect(box()).toHaveValue("Original");
  expect(send()).toBeEnabled();
});

it.each(["HTTP", "wrong seq", "assistant", "tool type", "non-string", "still preview"])("rejects %s detail without sending a fragment", async invalid => {
  const { sendMessage } = mount([prompt(1, "Preview…", true)]);
  edit("Original");
  arrow("ArrowUp");
  const body = { ...prompt(invalid === "wrong seq" ? 2 : 1, "Invalid full text"),
    ...(invalid === "assistant" ? { role: "assistant" } : {}),
    ...(invalid === "tool type" ? { type: "tool_result" } : {}),
    ...(invalid === "non-string" ? { content: {} } : {}),
    ...(invalid === "still preview" ? { body_state: "preview" } : {}) };
  await act(async () => reads[0].reply.resolve(Response.json(body, { status: invalid === "HTTP" ? 500 : 200 })));
  expect(box()).toHaveValue("Original");
  expect(send()).toBeEnabled();
  expect(screen.getByRole("alert")).toHaveTextContent(/history message/i);
  expect(sendMessage).not.toHaveBeenCalled();
  arrow("ArrowDown");
  expect(box()).toHaveValue("Original");
});

it.each(["/compact ", "/reset ", "/model another-model "])("blocks %s before rendering disabled and permits sending after editing", async command => {
  const { sendMessage } = mount([prompt(1, "Preview…", true)]);
  edit(command);
  const button = send();
  act(() => {
    box().dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true, cancelable: true }));
    button.click();
    box().dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
  });
  expect(reads).toHaveLength(1);
  expect(sendMessage).not.toHaveBeenCalled();
  expect(transport.mock.calls.filter(([, init]) => init?.method && init.method !== "GET")).toHaveLength(0);
  edit("Ready to send");
  fireEvent.click(send());
  await waitFor(() => expect(sendMessage).toHaveBeenCalledExactlyOnceWith(session.id, "Ready to send", undefined, undefined));
  await act(async () => reads[0].reply.resolve(Response.json(prompt(1, "Retired full message"))));
  expect(box()).toHaveValue("");
});
