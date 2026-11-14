import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { mockChatViewport } from "./chatViewport.test-support";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { sessions } from "../lib/sessionList";
import { deferred } from "../test-support/deferred";

const a = { id: "draft-chat-a", name: "A", status: "idle", backend: "codex", created_at: "2026-11-08", working_dir: "/tmp" } as SessionInfo;
const b = { ...a, id: "draft-chat-b", name: "B" };
const saved = 'octopus.text-draft.v1:["chat-owner","chat","shared"]';
const box = () => screen.getByRole<HTMLTextAreaElement>("textbox");
const edit = (value: string) => fireEvent.change(box(), { target: { value } });
const confirmIdentity = () => act(() => useSessionStore.getState().setIdentity({ user_id: "chat-owner", label: "Chat owner", is_admin: false }));
function mount(sendMessage = vi.fn<Parameters<typeof ChatView>[0]["sendMessage"]>()) {
  return { ...render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={sendMessage} interrupt={vi.fn()} answerQuestion={vi.fn()} connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider>), sendMessage };
}
beforeEach(() => {
  mockChatViewport();
  useSessionStore.getState().setToken("");
  sessionStorage.clear();
  useSessionStore.getState().setToken("chat-draft-test");
  useSessionStore.setState({ activeSessionId: a.id, sessions: [a, b], archivedSessions: [], agents: [], messages: {},
    pendingQuestions: {}, streamingText: {}, dispatchStates: {}, dispatchUnavailable: {}, pendingForks: {}, credentials: [], backendModels: {} });
  vi.stubGlobal("fetch", vi.fn(async () => Response.json([])));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); useSessionStore.getState().setToken(""); });

const arrow = (key: "ArrowUp" | "ArrowDown") => {
  box().setSelectionRange(box().value.length, box().value.length);
  fireEvent.keyDown(box(), { key });
};
const prompt = (seq: number, content: string) => ({ seq, role: "user" as const, type: "text", content });

it("ends history on every session change while preserving the shared draft", () => {
  useSessionStore.setState({ messages: { [a.id]: [prompt(0, "A first"), prompt(1, "A second"), prompt(2, "A third")],
    [b.id]: [prompt(0, "B only")] } });
  mount();
  edit("Unsent draft");
  arrow("ArrowUp");
  expect(box()).toHaveValue("A third");
  act(() => useSessionStore.setState({ activeSessionId: b.id }));
  arrow("ArrowUp");
  expect(box()).toHaveValue("B only");
  arrow("ArrowDown");
  expect(box()).toHaveValue("A third");
  act(() => useSessionStore.setState({ activeSessionId: a.id }));
  arrow("ArrowDown");
  expect(box()).toHaveValue("A third");
  arrow("ArrowUp");
  expect(box()).toHaveValue("A third");
  arrow("ArrowDown");
  expect(box()).toHaveValue("A third");
});

it("follows the selected message across insertion, and retires a missing candidate permanently", () => {
  const rows = [prompt(1, "First"), prompt(2, "Second"), prompt(3, "Third")];
  useSessionStore.setState({ messages: { [a.id]: rows } });
  mount();
  edit("Original draft");
  arrow("ArrowUp");
  arrow("ArrowUp");
  expect(box()).toHaveValue("Second");
  act(() => useSessionStore.setState({ messages: { [a.id]: [prompt(0, "Earlier"), ...rows.map(row => ({ ...row }))] } }));
  arrow("ArrowUp");
  expect(box()).toHaveValue("First");
  arrow("ArrowDown");
  arrow("ArrowDown");
  arrow("ArrowDown");
  expect(box()).toHaveValue("Original draft");
  arrow("ArrowUp");
  act(() => useSessionStore.setState({ messages: { [a.id]: [] } }));
  arrow("ArrowUp");
  expect(box()).toHaveValue("Third");
  act(() => useSessionStore.setState({ messages: { [a.id]: rows } }));
  arrow("ArrowDown");
  expect(box()).toHaveValue("Third");
});

it("restores one shared unsent text after identity confirmation and component remount without sending", () => {
  sessionStorage.setItem(saved, "Saved before refresh 中文");
  const view = mount();
  expect(box()).toHaveValue("");
  confirmIdentity();
  expect(box()).toHaveValue("Saved before refresh 中文");
  act(() => useSessionStore.setState({ activeSessionId: b.id }));
  expect(box()).toHaveValue("Saved before refresh 中文");
  edit("Latest shared edit");
  expect(sessionStorage.getItem(saved)).toBe("Latest shared edit");
  view.unmount();
  const next = mount();
  expect(box()).toHaveValue("Latest shared edit");
  expect(next.sendMessage).not.toHaveBeenCalled();
  expect(view.sendMessage).not.toHaveBeenCalled();
});

it.each([false, true])("remount never revives a consumed compact command, preserving later edits=%s", async newer => {
  confirmIdentity();
  const reply = deferred<Response>();
  const transport = vi.fn((_url: unknown, init?: RequestInit) => init?.method === "POST" ? reply.promise : Promise.resolve(Response.json([])));
  vi.stubGlobal("fetch", transport);
  const view = mount();
  edit("/compact");
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect(sessionStorage.getItem(saved)).toBeNull();
  expect(box()).toHaveValue("/compact");
  if (newer) edit("New unsent text");
  view.unmount();
  mount();
  expect(box()).toHaveValue(newer ? "New unsent text" : "");
  await act(async () => reply.resolve(Response.json({ accepted: true, operation: "compact" })));
  expect(box()).toHaveValue(newer ? "New unsent text" : "");
  expect(transport.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  expect(view.sendMessage).not.toHaveBeenCalled();
});

it.each([false, true])("confirmed current-session deletion clears the shared version even while unmounted=%s", unmounted => {
  confirmIdentity();
  const view = mount();
  edit("Delete with this conversation");
  if (unmounted) view.unmount();
  act(() => { sessions.remove(a.id); });
  expect(sessionStorage.getItem(saved)).toBeNull();
  act(() => useSessionStore.setState({ activeSessionId: b.id }));
  if (unmounted) mount();
  expect(box()).toHaveValue("");
  edit("Can still edit after deletion");
  expect(box()).toHaveValue("Can still edit after deletion");
  expect(sessionStorage.getItem(saved)).toBe("Can still edit after deletion");
});

it.each(["saved", "edited", "cleared", "empty"])("fork prefill waits for identity and respects %s shared text", mode => {
  if (mode === "saved") sessionStorage.setItem(saved, "Saved newer fork edit");
  act(() => useSessionStore.setState({ sessions: [{ ...a, fork_prefilled_prompt: "Original fork prompt" }, b] }));
  const view = mount();
  expect(box()).toHaveValue("");
  if (mode === "edited") edit("Typed before identity");
  if (mode === "cleared") { edit("Typed before identity"); edit(""); }
  confirmIdentity();
  const expected = mode === "saved" ? "Saved newer fork edit" : mode === "edited" ? "Typed before identity" : mode === "cleared" ? "" : "Original fork prompt";
  expect(box()).toHaveValue(expected);
  expect(sessionStorage.getItem(saved)).toBe(expected || null);
  view.unmount(); mount();
  expect(box()).toHaveValue(expected);
  expect(view.sendMessage).not.toHaveBeenCalled();
});

it("prefills a newly selected fork after an earlier ordinary message consumed the shared draft", () => {
  confirmIdentity(); const view = mount();
  edit("An earlier message");
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect(box()).toHaveValue("");
  act(() => useSessionStore.setState({ activeSessionId: b.id, sessions: [a, { ...b, fork_prefilled_prompt: "Rewound message" }] }));
  expect(box()).toHaveValue("Rewound message");
  expect(sessionStorage.getItem(saved)).toBe("Rewound message");
  expect(view.sendMessage).toHaveBeenCalledOnce();
});

it("does not overwrite a deliberate clear while fork metadata is still arriving", () => {
  confirmIdentity(); mount();
  edit("New input while opening"); edit("");
  act(() => useSessionStore.setState({ sessions: [{ ...a, fork_prefilled_prompt: "Late old prompt" }, b] }));
  expect(box()).toHaveValue("");
  expect(sessionStorage.getItem(saved)).toBeNull();
});

it("consumes a message synchronously and never clears a newer draft on its unknown receipt", async () => {
  confirmIdentity();
  const receipt = deferred<{ accepted: false; state: "unknown"; clientRequestId: string }>();
  const view = mount(vi.fn(() => receipt.promise));
  edit("Original message");
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect(sessionStorage.getItem(saved)).toBeNull();
  edit("New unsent question");
  await act(async () => receipt.resolve({ accepted: false, state: "unknown", clientRequestId: "original-id" }));
  expect(box()).toHaveValue("New unsent question");
  expect(sessionStorage.getItem(saved)).toBe("New unsent question");
  expect(screen.getByRole("button", { name: "Retry admission confirmation" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Restore draft" })).toBeDisabled();
  expect(view.sendMessage).toHaveBeenCalledOnce();
  edit("");
  expect(sessionStorage.getItem(saved)).toBeNull();
  view.unmount(); mount();
  expect(box()).toHaveValue("");
  expect(screen.queryByRole("button", { name: "Retry admission confirmation" })).toBeNull();
});

it("keeps the shared draft on archive and deletion of another session", () => {
  confirmIdentity(); mount(); edit("Keep shared draft");
  act(() => { sessions.archive(a.id); sessions.remove(b.id); });
  expect(sessionStorage.getItem(saved)).toBe("Keep shared draft");
  act(() => useSessionStore.setState({ sessions: [{ ...b, id: "next-chat" }], activeSessionId: "next-chat" }));
  expect(box()).toHaveValue("Keep shared draft");
});

it("clears the old account's saved scope and cannot revive it across account ABA", () => {
  confirmIdentity(); mount(); edit("Private account A");
  act(() => {
    useSessionStore.getState().setToken("different-owner");
    useSessionStore.getState().setIdentity({ user_id: "other-owner", label: "Other", is_admin: false });
    useSessionStore.setState({ activeSessionId: b.id, sessions: [b] });
  });
  expect(sessionStorage.getItem(saved)).toBeNull();
  expect(box()).toHaveValue("");
  edit("Private account B");
  act(() => {
    useSessionStore.getState().setToken("chat-draft-test");
    useSessionStore.getState().setIdentity({ user_id: "chat-owner", label: "First", is_admin: false });
    useSessionStore.setState({ activeSessionId: a.id, sessions: [a] });
  });
  expect(box()).toHaveValue("");
  expect(Object.keys(sessionStorage).filter(key => key.startsWith("octopus.text-draft.v1:"))).toEqual([]);
});

it.each(["getItem", "setItem"] as const)("reports storage %s denial while preserving editing and explicit sending", method => {
  confirmIdentity();
  vi.spyOn(Storage.prototype, method).mockImplementation(() => { throw new DOMException("denied", "SecurityError"); });
  const view = mount();
  if (method === "getItem") expect(screen.getByText(/Text draft could not be saved/)).toBeVisible();
  edit("Still sendable");
  if (method === "setItem") expect(screen.getByText(/Text draft could not be saved/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect(view.sendMessage).toHaveBeenCalledWith(a.id, "Still sendable", undefined, undefined);
  expect(box()).toHaveValue("");
});

it("keeps compact's original GET identity without clearing a newly edited version on confirmation", async () => {
  confirmIdentity();
  const requests: { url: string; method?: string }[] = [];
  let accepted = false;
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    const path = String(url);
    if (!path.includes("/submissions") || (!init?.method && !path.includes("/submissions/"))) return Response.json([]);
    requests.push({ url: path, method: init?.method });
    if (init?.method === "POST") throw new TypeError("lost receipt");
    return accepted ? Response.json({ accepted: true, operation: "compact" }) : new Response("Missing", { status: 404 });
  }));
  mount(); edit("/compact");
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  const confirm = await screen.findByRole("button", { name: "Retry admission confirmation" });
  expect(sessionStorage.getItem(saved)).toBeNull();
  expect(box()).toHaveValue("/compact");
  edit("New question"); edit("/compact"); // Even the same characters are a new version.
  accepted = true;
  fireEvent.click(confirm);
  await waitFor(() => expect(screen.queryByRole("button", { name: "Retry admission confirmation" })).toBeNull());
  expect(box()).toHaveValue("/compact");
  expect(sessionStorage.getItem(saved)).toBe("/compact");
  expect(requests.map(request => request.method)).toEqual(["POST", "GET", "GET"]);
  expect(requests[2].url).toBe(requests[1].url);
});
