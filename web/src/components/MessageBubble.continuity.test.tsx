import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MessageBubble } from "./MessageBubble";
import { nativeChatMessages } from "../lib/nativeOutput";
import { useSessionStore, type Message, type SubagentRun } from "../stores/sessionStore";

const runtime = { request_id: "work-request", attempt_id: "work-attempt", native: {
  thread_id: "helper-thread", turn_id: "helper-turn", item_id: "helper-answer",
  root_thread_id: "root-thread", root_turn_id: "root-turn",
} };
const child: Message = { role: "assistant", type: "text", seq: 10,
  content: "Collected answer from this work", runtime_metadata: runtime };
const saved: Message = { role: "system", type: "native_subagent", seq: 11,
  runtime_metadata: runtime, tool_input: { card_id: "saved-card", task_id: "helper-thread",
    native_thread_id: "helper-thread", native_turn_id: "helper-turn", status: "completed",
    name: "Saved helper", description: "Same observed work", steps: [] } };

beforeEach(() => {
  useSessionStore.setState({ token: "", accountGeneration: 1, messages: {}, messageWindows: {},
    streamingItems: {}, subagents: {} });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("keeps the mounted card and expansion when the same observed work gains its saved identity", () => {
  useSessionStore.getState().setMessages("s", [child]);
  const originalWindow = useSessionStore.getState().messageWindows.s;
  const projected = () => nativeChatMessages(useSessionStore.getState().messages.s)[0];
  const view = render(<MessageBubble sessionId="s" message={projected()} />);
  const shell = view.container.querySelector(".subagent-card");
  const toggle = view.container.querySelector<HTMLButtonElement>(".subagent-summary-row")!;
  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  act(() => {
    useSessionStore.getState().setMessages("s", [child, saved]);
    view.rerender(<MessageBubble sessionId="s" message={projected()} />);
  });
  expect(useSessionStore.getState().messageWindows.s, "this is a continuous window update").toBe(originalWindow);
  expect(view.container.querySelector(".subagent-card"), "saving the same work preserves its DOM shell").toBe(shell);
  expect(view.container.querySelector(".subagent-summary-row")).toHaveAttribute("aria-expanded", "true");
  expect(view.getByText("Saved helper")).toBeVisible();
  expect(view.getByText("Collected answer from this work")).toBeVisible();
});

it("keeps an expanded same-work card through inline to preview without automatically loading its full record", () => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  useSessionStore.getState().setMessages("s", [child, saved]);
  const originalWindow = useSessionStore.getState().messageWindows.s;
  const view = render(<MessageBubble sessionId="s" message={saved} />);
  fireEvent.click(view.container.querySelector<HTMLButtonElement>(".subagent-summary-row")!);
  const shell = view.container.querySelector(".subagent-card");
  const preview: Message = { ...saved, body_state: "preview", detail_ref: "/api/sessions/s/messages/11" };
  act(() => {
    useSessionStore.getState().setMessages("s", [child, preview]);
    view.rerender(<MessageBubble sessionId="s" message={preview} />);
  });
  expect(useSessionStore.getState().messageWindows.s).toBe(originalWindow);
  expect(view.container.querySelector(".subagent-card")).toBe(shell);
  expect(view.container.querySelector(".subagent-summary-row")).toHaveAttribute("aria-expanded", "true");
  expect(view.getByRole("button", { name: "View full message" })).toBeVisible();
  expect(fetch).not.toHaveBeenCalled();
});

it("does not infer the same work from a reused cardId and name when native identity is unavailable", () => {
  const run: SubagentRun = { card_id: "reused", task_id: "helper-thread", native_thread_id: "helper-thread",
    native_turn_id: "helper-turn", name: "Same helper", status: "running", description: "Working",
    prompt: "", summary: "First observation", steps: [] };
  useSessionStore.getState().setSubagents("s", [run]);
  const projected = () => nativeChatMessages([], Object.values(useSessionStore.getState().subagents.s))[0];
  const view = render(<MessageBubble sessionId="s" message={projected()} />);
  fireEvent.click(view.container.querySelector<HTMLButtonElement>(".subagent-summary-row")!);
  expect(view.container.querySelector(".subagent-summary-row")).toHaveAttribute("aria-expanded", "true");
  act(() => {
    useSessionStore.getState().setSubagents("s", [{ ...run, summary: "Another observation with insufficient identity" }]);
    view.rerender(<MessageBubble sessionId="s" message={projected()} />);
  });
  expect(view.container.querySelector(".subagent-summary-row")).toHaveAttribute("aria-expanded", "false");
  expect(view.getByText("Another observation with insufficient identity")).toBeVisible();
});

const liveRun: SubagentRun = { card_id: "saved-card", task_id: "helper-thread", native_thread_id: "helper-thread",
  native_turn_id: "helper-turn", name: "Same helper", status: "running", description: "Working", prompt: "",
  summary: "Live work", steps: [], runtime_metadata: runtime };
const projectedWork = (sessionId: string) => {
  const state = useSessionStore.getState();
  return nativeChatMessages(state.messages[sessionId] ?? [], Object.values(state.subagents[sessionId] ?? {}))
    .find((message) => message.type === "native_subagent")!;
};

it("keeps a mounted live snapshot open when its saved seq and detail reference arrive", () => {
  const root: Message = { role: "assistant", type: "text", seq: 1, content: "Root progress" };
  useSessionStore.getState().setMessages("s", [root]);
  useSessionStore.getState().setSubagents("s", [liveRun]);
  const original = useSessionStore.getState().messageWindows.s;
  const view = render(<MessageBubble sessionId="s" message={projectedWork("s")} />);
  fireEvent.click(view.container.querySelector<HTMLButtonElement>(".subagent-summary-row")!);
  const shell = view.container.querySelector(".subagent-card");
  expect(view.getByText(/No saved reference/)).toBeVisible();
  act(() => {
    useSessionStore.getState().setMessages("s", [root, { ...saved, body_state: "preview", detail_ref: "/api/sessions/s/messages/11" }]);
    view.rerender(<MessageBubble sessionId="s" message={projectedWork("s")} />);
  });
  expect(useSessionStore.getState().messageWindows.s).toBe(original);
  expect(view.container.querySelector(".subagent-card")).toBe(shell);
  expect(view.container.querySelector(".subagent-summary-row")).toHaveAttribute("aria-expanded", "true");
  expect(view.getByRole("button", { name: "View full message" })).toBeVisible();
  expect(view.queryByText(/No saved reference/)).not.toBeInTheDocument();
});

it.each(["request", "attempt", "thread", "turn", "account", "session"])("does not keep expansion across a different %s", (boundary) => {
  useSessionStore.getState().setSubagents("s", [liveRun]);
  const view = render(<MessageBubble sessionId="s" message={projectedWork("s")} />);
  fireEvent.click(view.container.querySelector<HTMLButtonElement>(".subagent-summary-row")!);
  let sessionId = "s";
  let next = liveRun;
  if (boundary === "request") next = { ...liveRun, runtime_metadata: { ...runtime, request_id: "other-request" } };
  if (boundary === "attempt") next = { ...liveRun, runtime_metadata: { ...runtime, attempt_id: "other-attempt" } };
  if (boundary === "thread") next = { ...liveRun, native_thread_id: "other-thread",
    runtime_metadata: { ...runtime, native: { ...runtime.native, thread_id: "other-thread" } } };
  if (boundary === "turn") next = { ...liveRun, native_turn_id: "other-turn",
    runtime_metadata: { ...runtime, native: { ...runtime.native, turn_id: "other-turn" } } };
  act(() => {
    if (boundary === "account") useSessionStore.getState().setToken("other-account");
    if (boundary === "session") sessionId = "other-session";
    useSessionStore.getState().setSubagents(sessionId, [next]);
    view.rerender(<MessageBubble sessionId={sessionId} message={projectedWork(sessionId)} />);
  });
  expect(view.container.querySelector(".subagent-summary-row")).toHaveAttribute("aria-expanded", "false");
});

it("preserves the same mounted work while its detail resources retire independently through a history prepend", async () => {
  useSessionStore.setState({ token: "detail-reader" });
  const preview: Message = { ...saved, body_state: "preview", detail_ref: "/api/sessions/s/messages/11" };
  useSessionStore.getState().setMessages("s", [child, preview]);
  useSessionStore.getState().setMessageWindow("s", 10, true);
  const original = useSessionStore.getState().messageWindows.s;
  const bodies = new Map<number, (response: Response) => void>();
  const fullReads: number[] = [];
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://localhost");
    const seq = Number(url.pathname.split("/").at(-1));
    if (url.pathname.includes("/native-details/")) return Promise.resolve(Response.json({ anchor_seq: seq,
      child: liveRun, availability: "available", messages: [{ role: "assistant", type: "text", seq: 10,
        content: `Native detail ${seq}` }], next_after_seq: null, has_more: false }));
    fullReads.push(seq);
    return new Promise<Response>((resolve) => { bodies.set(seq, resolve); });
  }));
  const view = render(<MessageBubble sessionId="s" message={projectedWork("s")} />);
  fireEvent.click(view.container.querySelector<HTMLButtonElement>(".subagent-summary-row")!);
  expect(await view.findByText("Native detail 11")).toBeVisible();
  const shell = view.container.querySelector(".subagent-card");
  fireEvent.click(view.getByRole("button", { name: "View full message" }));
  expect(view.getByText("Loading full message…")).toBeVisible();
  const earlier: Message = { ...preview, seq: 5, detail_ref: "/api/sessions/s/messages/5" };
  act(() => {
    const result = useSessionStore.getState().prependMessages("s", [earlier], 5, false, { beforeSeq: 10, window: original });
    expect(result.state).toBe("merged");
    view.rerender(<MessageBubble sessionId="s" message={projectedWork("s")} />);
  });
  expect(useSessionStore.getState().messageWindows.s).toBe(original);
  expect(view.container.querySelector(".subagent-card")).toBe(shell);
  expect(view.container.querySelector(".subagent-summary-row")).toHaveAttribute("aria-expanded", "true");
  expect(view.queryByText("Loading full message…")).not.toBeInTheDocument();
  expect(view.queryByText("Native detail 11")).not.toBeInTheDocument();
  expect(await view.findByText("Native detail 5")).toBeVisible();
  const expand = view.getByRole("button", { name: "View full message" });
  expect(expand).toBeEnabled();
  expect(fullReads).toEqual([11]);
  fireEvent.click(expand);
  await act(async () => { bodies.get(11)!(Response.json({ ...saved, tool_input: { ...saved.tool_input, name: "Retired full record" } })); });
  expect(view.getByText("Loading full message…")).toBeVisible();
  expect(view.queryByText(/Retired full record/)).not.toBeInTheDocument();
  await act(async () => { bodies.get(5)!(Response.json({ ...saved, seq: 5, tool_input: { ...saved.tool_input, name: "Current full record" } })); });
  expect(view.getByText(/Current full record/)).toBeVisible();
  expect(view.queryByText(/Retired full record/)).not.toBeInTheDocument();
  expect(fullReads).toEqual([11, 5]);
  expect(useSessionStore.getState().messages.s.find((message) => message.seq === 5)?.tool_input?.name).toBe("Saved helper");
});
