import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { Popover } from "./ui/popover";
import { mockChatViewport } from "./chatViewport.test-support";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";

const a = { id: "key-a", name: "A", status: "running", backend: "codex", working_dir: "/tmp", created_at: "now" } as SessionInfo;
const b = { ...a, id: "key-b", name: "B" };
beforeEach(() => {
  mockChatViewport();
  useSessionStore.getState().setToken("");
  useSessionStore.getState().setToken("key-test");
  useSessionStore.setState({ mainView: "chat", activeSessionId: a.id, sessions: [a, b], archivedSessions: [], agents: [], messages: {},
    pendingQuestions: {}, streamingText: {}, dispatchStates: {}, dispatchUnavailable: {}, pendingForks: {}, credentials: [], backendModels: {} });
  vi.stubGlobal("fetch", vi.fn(async (url: unknown) => Response.json(String(url).endsWith("/submissions") ? { items: [], next_cursor: null } : [])));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
async function mount(extra?: React.ReactNode) {
  const interrupt = vi.fn();
  await act(async () => { render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={vi.fn()} interrupt={interrupt} answerQuestion={vi.fn()} connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
    {extra}
  </VirtuosoMockContext.Provider>); });
  expect(screen.getByRole("button", { name: "Stop" })).toBeVisible();
  return interrupt;
}

it.each(["hidden", "composing", "229", "consumed"])("does not stop the running chat for a %s Escape", async cause => {
  const interrupt = await mount();
  const input = screen.getByRole("textbox");
  fireEvent.change(input, { target: { value: "Keep this draft" } });
  if (cause === "hidden") act(() => useSessionStore.setState({ mainView: "schedules" }));
  const event = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true,
    isComposing: cause === "composing", keyCode: cause === "229" ? 229 : 27 });
  if (cause === "consumed") event.preventDefault();
  fireEvent(input, event);
  expect(interrupt).not.toHaveBeenCalled();
  expect(input).toHaveValue("Keep this draft");
});

it("targets the current foreground session even before React commits the selection", async () => {
  const interrupt = await mount();
  act(() => {
    useSessionStore.setState({ activeSessionId: b.id });
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", cancelable: true }));
  });
  expect(interrupt).toHaveBeenCalledExactlyOnceWith(b.id);
});

it("consumes the same Escape that closes a form popover", async () => {
  const close = vi.fn();
  const interrupt = await mount(<Popover open onClose={close} label="Foreground form" trigger={<button>Open form</button>}><button>Inside form</button></Popover>);
  fireEvent.keyDown(screen.getByRole("button", { name: "Inside form" }), { key: "Escape" });
  expect(close).toHaveBeenCalledTimes(1);
  expect(interrupt).not.toHaveBeenCalled();
});
