import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ChatView } from "./ChatView";
import { mockChatViewport } from "./chatViewport.test-support";
import { VirtuosoMockContext } from "react-virtuoso";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { schedules } from "../lib/scheduleList";
import commandCasesText from "../../../tests/fixtures/wiki-command-cases.json?raw";

const commandCases: { message: string; mode: "read" | "maintain" | null }[] = JSON.parse(commandCasesText);

const session = {
  id: "wiki-session", name: "Wiki chat", working_dir: "/tmp",
  created_at: "2026-10-12", status: "idle", backend: "codex",
} as SessionInfo;

beforeEach(() => {
  mockChatViewport();
  useSessionStore.setState({
    token: "",
    activeSessionId: session.id, sessions: [session], agents: [],
    messages: {}, streamingText: {}, dispatchUnavailable: {}, pendingQuestions: {},
    dispatchStates: {}, dispatchGeneration: null, retiredDispatchGenerations: [],
    pendingForks: {}, credentials: [], backendModels: {},
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("does not restore schedules deleted during a slash-command creation", async () => {
  useSessionStore.getState().setToken("schedule-token");
  const old = { id: "old", name: "old", enabled: true } as ReturnType<typeof useSessionStore.getState>["schedules"][number];
  useSessionStore.setState({ sessions: [{ ...session, agent_id: "owner" }], activeSessionId: session.id, schedules: [old] });
  useSessionStore.getState().applyDispatchState(session.id, {
    generation: "schedule-service", revision: 1, paused: false, active_request_id: null, items: [],
  });
  let finish!: (response: Response) => void;
  let serverRows = [old];
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST" && url.endsWith("/from_text")) return new Promise<Response>((resolve) => { finish = resolve; });
    return Response.json(url.endsWith("/api/schedules") ? serverRows : []);
  });
  vi.stubGlobal("fetch", fetch);
  try {
    const { input, send } = setup();
    fireEvent.change(input, { target: { value: "/schedule tomorrow remind me" } });
    fireEvent.click(send);
    await waitFor(() => expect(fetch.mock.calls.some(([url]) => url.endsWith("/from_text"))).toBe(true));
    serverRows = [];
    await act(async () => { schedules.changed(); });
    await waitFor(() => expect(useSessionStore.getState().schedules).toEqual([]));
    const created = { ...old, id: "new", name: "Reminder", recurrence_label: "Tomorrow" };
    serverRows = [created];
    await act(async () => { finish(Response.json(created)); });
    await waitFor(() => expect(useSessionStore.getState().schedules.map((row) => row.id)).toEqual(["new"]));
    expect(useSessionStore.getState().messages[session.id].some((message) => String(message.content).includes('Scheduled "Reminder"'))).toBe(true);
    const call = fetch.mock.calls.find(([url]) => url.endsWith("/from_text"))!;
    expect(call[0]).toContain("/api/agents/owner/");
    expect(JSON.parse(String(call[1]?.body))).toMatchObject({ session_id: session.id, text: "tomorrow remind me", timezone: expect.any(String) });
  } finally { act(() => useSessionStore.getState().setToken("")); vi.unstubAllGlobals(); }
});

it("keeps correction separate from paused results and resumes only on the explicit button", async () => {
  const paused = {
    generation: "service-ui", revision: 1, paused: true, active_request_id: "correction",
    items: [{ request_id: "bg-1", source: "bg", summary: "background result", priority: false }],
  };
  useSessionStore.getState().applyDispatchState(session.id, paused);
  const resume = vi.fn(async () => new Response(JSON.stringify({ ...paused, revision: 2, paused: false })));
  vi.stubGlobal("fetch", resume);
  const { input, send, sendMessage } = setup();
  expect(screen.getByText("Paused · 1 pending")).toBeTruthy();
  expect(screen.getByText("Background task")).toBeTruthy();
  fireEvent.change(input, { target: { value: "correct the direction" } });
  fireEvent.click(send);
  expect(sendMessage).toHaveBeenCalledWith(session.id, "correct the direction", undefined, undefined);
  expect(useSessionStore.getState().dispatchStates[session.id].paused).toBe(true);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Resume processing" })));
  expect(useSessionStore.getState().dispatchStates[session.id].paused).toBe(false);
  expect(resume).toHaveBeenCalledWith(expect.stringContaining(`/api/sessions/${session.id}/dispatch/resume`),
    expect.objectContaining({ method: "POST" }));
  vi.unstubAllGlobals();
});

it("shows pause independently while a correction runs, including an empty pending queue", () => {
  useSessionStore.setState({ sessions: [{ ...session, status: "running" }] });
  useSessionStore.getState().applyDispatchState(session.id, {
    generation: "service-ui", revision: 1, paused: true, active_request_id: "correction", items: [],
  });
  const sendMessage = vi.fn();
  render(<ChatView sendMessage={sendMessage} interrupt={vi.fn()}
    answerQuestion={vi.fn()} connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />);
  expect(screen.getByText("running")).toBeTruthy();
  expect(screen.getByText("Paused · 0 pending")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Resume processing" })).toBeTruthy();
  expect(screen.getByText(/Your next message takes priority/)).toBeTruthy();
  expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
});

it("keeps the queue paused and reports a failed resume", async () => {
  useSessionStore.getState().applyDispatchState(session.id, {
    generation: "service-ui", revision: 1, paused: true, active_request_id: null, items: [],
  });
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "Turn quota exceeded" }), { status: 429 })));
  setup();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Resume processing" })));
  expect(screen.getByRole("alert")).toHaveTextContent("Turn quota exceeded");
  expect(useSessionStore.getState().dispatchStates[session.id].paused).toBe(true);
  expect(screen.getByRole("button", { name: "Resume processing" })).not.toBeDisabled();
  vi.unstubAllGlobals();
});

function setup(send?: Parameters<typeof ChatView>[0]["sendMessage"]) {
  const sendMessage = vi.fn(send);
  render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}><ChatView sendMessage={sendMessage} interrupt={vi.fn()}
    answerQuestion={vi.fn()}
    connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} /></VirtuosoMockContext.Provider>);
  return {
    sendMessage,
    input: screen.getByRole("textbox"),
    send: screen.getByRole("button", { name: "Send message" }),
  };
}

it("keeps the draft and explains why it cannot send without secure randomness", async () => {
  vi.stubGlobal("crypto", undefined);
  try {
    const { input, send, sendMessage } = setup();
    fireEvent.change(input, { target: { value: "Keep this input" } });
    await act(async () => fireEvent.click(send));
    expect(screen.getByRole("alert")).toHaveTextContent(/secure randomness/i);
    expect(input).toHaveValue("Keep this input");
    expect(sendMessage).not.toHaveBeenCalled();
  } finally {
    vi.unstubAllGlobals();
  }
});

it("preserves a new selection made immediately after slash completion", () => {
  const frames: FrameRequestCallback[] = [];
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => frames.push(callback));
  try {
    const { input } = setup();
    const field = input as HTMLTextAreaElement;
    act(() => frames.splice(0).forEach((callback) => callback(0)));
    fireEvent.change(input, { target: { value: "/rem" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(field.value).toBe("/remember ");
    expect(field.selectionStart).toBe(field.value.length);
    field.setSelectionRange(0, field.value.length);
    act(() => frames.splice(0).forEach((callback) => callback(0)));
    expect(field.selectionStart).toBe(0);
    expect(field.selectionEnd).toBe(field.value.length);
    fireEvent.change(input, { target: { value: "/re" } });
    expect(screen.getByRole("listbox", { name: "Slash commands" })).toBeVisible();
  } finally {
    vi.unstubAllGlobals();
  }
});

it("submits and restores rejected drafts with secure bytes when randomUUID is absent", async () => {
  vi.stubGlobal("crypto", { getRandomValues: globalThis.crypto.getRandomValues.bind(globalThis.crypto) });
  try {
    const sendMessage = vi.fn(async () => ({ accepted: false, message: "Session not found" }));
    render(<ChatView sendMessage={sendMessage} interrupt={vi.fn()}
      answerQuestion={vi.fn()} connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "retained draft" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Send message" })));
    expect(sendMessage).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Restore draft" }));
    expect(screen.getByRole("textbox")).toHaveValue("retained draft");
  } finally {
    vi.unstubAllGlobals();
  }
});

it("keeps a rejected Wiki draft and protects subsequent editing during restoration", async () => {
  let finish!: (reply: { accepted: boolean; message: string }) => void;
  const sendMessage = vi.fn(() => new Promise<{ accepted: boolean; message: string }>((resolve) => { finish = resolve; }));
  render(<ChatView sendMessage={sendMessage} interrupt={vi.fn()}
    answerQuestion={vi.fn()} connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />);
  const input = screen.getByRole("textbox");
  fireEvent.change(input, { target: { value: "  /wiki maintain Library/a.md  " } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  fireEvent.change(input, { target: { value: "new draft" } });
  await act(async () => finish({ accepted: false, message: "Conversation is closed" }));
  expect(input).toHaveValue("new draft");
  expect(screen.getByRole("alert")).toHaveTextContent("Conversation is closed");
  expect(screen.getByRole("button", { name: "Restore draft" })).toBeDisabled();
  fireEvent.change(input, { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Restore draft" }));
  expect(input).toHaveValue("  /wiki maintain Library/a.md  ");
  expect(screen.queryByRole("button", { name: "Use local Wiki for this message" })).not.toBeInTheDocument();
  expect(sendMessage).toHaveBeenCalledTimes(1);
});

it("retains a service-not-ready draft for explicit retry without treating it as unknown", async () => {
  const sendMessage = vi.fn(async () => ({ accepted: false, code: "service_not_ready", message: "Service input admission is not ready (initializing)." }));
  const { input, send } = setup(sendMessage);
  fireEvent.change(input, { target: { value: "Wait for recovery 中文" } });
  await act(async () => fireEvent.click(send));
  fireEvent.click(screen.getByRole("button", { name: "Restore draft" }));
  expect(input).toHaveValue("Wait for recovery 中文");
  expect(sendMessage).toHaveBeenCalledOnce();
});

it("retains an unconfirmed draft after a missing receipt and confirms the original identity without resending", async () => {
  useSessionStore.setState({ token: "confirmation-account" });
  let reads = 0;
  const statusUrl = `${window.location.origin}/api/sessions/${session.id}/submissions/uncertain-client`;
  const request = vi.fn(async (url: string) => {
    if (url === statusUrl) {
      reads += 1;
      if (reads === 1) return Response.json({ detail: "Submission not found" }, { status: 404 });
      return Response.json({ session_id: session.id, client_request_id: "uncertain-client", request_id: "original-request",
        accepted: true, execution: { state: "running", state_version: 1, execution_owner_request_id: "original-request",
          delivery_state: "none", recoverable_input: false } });
    }
    if (url.includes("/wiki-jobs")) return Response.json([]);
    return Response.json({ items: [], next_cursor: null });
  });
  vi.stubGlobal("fetch", request);
  try {
    const { input, send, sendMessage } = setup(async () => ({
      accepted: false, state: "unknown", clientRequestId: "uncertain-client", message: "Admission unconfirmed",
    }));
    fireEvent.change(input, { target: { value: "Keep the original input" } });
    await act(async () => fireEvent.click(send));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry admission confirmation" })));
    expect(request).toHaveBeenCalledWith(statusUrl, {
      method: "GET", headers: { Authorization: "Bearer confirmation-account" },
    });
    expect(screen.getByRole("button", { name: "Restore draft" })).toBeDisabled();
    expect(screen.getByText("Keep the original input")).toBeInTheDocument();
    expect(screen.getByText(/Admission is still unconfirmed.*\(404\)/)).toBeInTheDocument();
    expect(sendMessage).toHaveBeenCalledTimes(1);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry admission confirmation" })));
    expect(reads).toBe(2);
    expect(sendMessage).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Restore draft" })).not.toBeInTheDocument();
    expect(screen.queryByText("Keep the original input")).not.toBeInTheDocument();
  } finally {
    vi.unstubAllGlobals();
  }
});

it("keeps an unknown execution through a failed status read and restores only a confirmed withdrawn input", async () => {
  useSessionStore.setState({ token: "confirmation-account" });
  let reads = 0;
  const statusUrl = `${window.location.origin}/api/sessions/${session.id}/submissions/uncertain-client`;
  const request = vi.fn(async (url: string) => {
    if (url === statusUrl) {
      reads += 1;
      if (reads === 2) throw new TypeError("Network unavailable");
      const unknown = reads === 1;
      return Response.json({ session_id: session.id, client_request_id: "uncertain-client", request_id: "original-request",
        accepted: true, message: unknown ? "Execution outcome remains unknown" : "Withdrawn before execution; input can be restored",
        execution: { state: unknown ? "unknown" : "withdrawn", state_version: reads,
          execution_owner_request_id: "original-request", delivery_state: "none", recoverable_input: !unknown } });
    }
    if (url.includes("/wiki-jobs")) return Response.json([]);
    return Response.json({ items: [], next_cursor: null });
  });
  vi.stubGlobal("fetch", request);
  try {
    let submissions = 0;
    const { input, send, sendMessage } = setup(async () => ++submissions === 1
      ? { accepted: false, state: "unknown", clientRequestId: "uncertain-client", message: "Admission unconfirmed" }
      : { accepted: true });
    fireEvent.change(input, { target: { value: "Review before retrying" } });
    await act(async () => fireEvent.click(send));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry admission confirmation" })));
    expect(screen.getByText("Execution outcome remains unknown")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restore draft" })).toBeDisabled();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry admission confirmation" })));
    expect(screen.getByText(/Admission is still unconfirmed.*Network unavailable/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restore draft" })).toBeDisabled();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry admission confirmation" })));
    expect(reads).toBe(3);
    expect(sendMessage).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Withdrawn before execution; input can be restored")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry admission confirmation" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Restore draft" }));
    expect(input).toHaveValue("Review before retrying");
    expect(sendMessage).toHaveBeenCalledTimes(1);
    fireEvent.change(input, { target: { value: "Reviewed recovery input" } });
    await act(async () => fireEvent.click(send));
    expect(sendMessage).toHaveBeenLastCalledWith(session.id, "Reviewed recovery input", undefined, undefined, undefined, "uncertain-client");
  } finally {
    vi.unstubAllGlobals();
  }
});

it("can read admission status while the WebSocket is disconnected", async () => {
  useSessionStore.setState({ token: "confirmation-account" });
  const statusUrl = `${window.location.origin}/api/sessions/${session.id}/submissions/uncertain-client`;
  const request = vi.fn(async (url: string) => {
    if (url === statusUrl) return Response.json({ session_id: session.id, client_request_id: "uncertain-client", request_id: "original-request",
      accepted: true, execution: { state: "done", state_version: 2, execution_owner_request_id: "original-request",
        delivery_state: "confirmed", recoverable_input: false } });
    if (url.includes("/wiki-jobs")) return Response.json([]);
    return Response.json({ items: [], next_cursor: null });
  });
  vi.stubGlobal("fetch", request);
  try {
    const sendMessage = vi.fn(async () => ({ accepted: false, state: "unknown" as const,
      clientRequestId: "uncertain-client", message: "Admission unconfirmed" }));
    const props = { sendMessage, interrupt: vi.fn(), answerQuestion: vi.fn(), onToggleSidebar: vi.fn(), onOpenSchedules: vi.fn() };
    const view = render(<ChatView {...props} connected />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Retain this input" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Send message" })));
    view.rerender(<ChatView {...props} connected={false} />);
    expect(screen.getByRole("button", { name: "Retry admission confirmation" })).not.toBeDisabled();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry admission confirmation" })));
    expect(request).toHaveBeenCalledWith(statusUrl, {
      method: "GET", headers: { Authorization: "Bearer confirmation-account" },
    });
    expect(sendMessage).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Retain this input")).not.toBeInTheDocument();
  } finally {
    vi.unstubAllGlobals();
  }
});

it("keeps a withdrawn receipt read-only when the server does not allow input recovery", async () => {
  useSessionStore.setState({ token: "confirmation-account" });
  const statusUrl = `${window.location.origin}/api/sessions/${session.id}/submissions/uncertain-client`;
  const request = vi.fn(async (url: string) => {
    if (url === statusUrl) return Response.json({ session_id: session.id, client_request_id: "uncertain-client", request_id: "original-request",
      accepted: true, message: "This historical receipt is read-only",
      execution: { state: "withdrawn", state_version: 2, execution_owner_request_id: "original-request",
        delivery_state: "none", recoverable_input: false } });
    if (url.includes("/wiki-jobs")) return Response.json([]);
    return Response.json({ items: [], next_cursor: null });
  });
  vi.stubGlobal("fetch", request);
  try {
    const { input, send, sendMessage } = setup(async () => ({
      accepted: false, state: "unknown", clientRequestId: "uncertain-client", message: "Admission unconfirmed",
    }));
    fireEvent.change(input, { target: { value: "Retain the read-only input" } });
    await act(async () => fireEvent.click(send));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry admission confirmation" })));
    expect(screen.getByText("This historical receipt is read-only")).toBeInTheDocument();
    expect(screen.getByText("Retain the read-only input")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restore draft" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Restore draft" }));
    expect(input).toHaveValue("");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry admission confirmation" })));
    expect(request.mock.calls.filter(([url]) => url === statusUrl)).toHaveLength(2);
    expect(sendMessage).toHaveBeenCalledTimes(1);
  } finally {
    vi.unstubAllGlobals();
  }
});

describe("shared Wiki without a composer switch", () => {
  it.each(["请结合共享知识解释这个问题", ...commandCases.map(({ message }) => message)])(
    "sends the original request without an implicit Wiki mode: %s", (message) => {
      const { input, send, sendMessage } = setup();
      expect(screen.queryByRole("button", { name: "Use local Wiki for this message" })).not.toBeInTheDocument();
      fireEvent.change(input, { target: { value: message } });
      fireEvent.click(send);
      expect(sendMessage).toHaveBeenLastCalledWith(session.id, message.trim(), undefined, undefined);
      expect(input).toHaveValue("");
    },
  );

  it.each(["普通查询", "/wiki 查询资料", "/wiki maintain Library/a.md"])(
    "keeps an unsent draft across conversation switches: %s", (message) => {
      const { input, send, sendMessage } = setup();
      fireEvent.change(input, { target: { value: message } });
      act(() => useSessionStore.setState({
        activeSessionId: "other-session",
        sessions: [session, { ...session, id: "other-session" }],
      }));
      expect(input).toHaveValue(message);
      fireEvent.click(send);
      expect(sendMessage).toHaveBeenLastCalledWith("other-session", message, undefined, undefined);
    },
  );

  it("does not send an empty request", () => {
    const { input, sendMessage } = setup();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(sendMessage).not.toHaveBeenCalled();
  });
});

describe("session-owned attachment drafts", () => {
  const other = { ...session, id: "other-session" };
  let uploads: { sessionId: string; finish: (response: Response) => void; file: File }[];

  beforeEach(() => {
    uploads = [];
    vi.stubGlobal("fetch", vi.fn((url: string, options: RequestInit) => new Promise<Response>((finish) => {
      const sessionId = /\/sessions\/([^/]+)\/attachments$/.exec(url)?.[1];
      if (!sessionId) throw new Error(`Unexpected fetch: ${url}`);
      uploads.push({ sessionId, finish, file: (options.body as FormData).get("file") as File });
    })));
  });
  afterEach(() => vi.unstubAllGlobals());

  const pick = () => fireEvent.change(screen.getByTestId("attachment-file-input"), {
    target: { files: [new File(["Required evidence"], "evidence.txt", { type: "text/plain" })] },
  });

  it.each(["selection", "drop", "paste"])("reports over-limit files from %s before sending", (method) => {
    const { input, send } = setup();
    const files = Array.from({ length: 11 }, (_, i) => new File(["x"], `evidence-${i}.txt`));
    if (method === "selection") fireEvent.change(screen.getByTestId("attachment-file-input"), { target: { files } });
    if (method === "drop") fireEvent.drop(input, { dataTransfer: { files } });
    if (method === "paste") fireEvent.paste(input, { clipboardData: { files } });
    expect(uploads).toHaveLength(10);
    expect(send).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("evidence-10.txt: At most 10 attachments per message");
    fireEvent.click(screen.getByRole("button", { name: "Continue with attached files" }));
    expect(screen.queryByText("These files were not attached:")).toBeNull();
  });
  const switchTo = (id: string) => act(() => useSessionStore.setState({
    sessions: [session, other], activeSessionId: id,
  }));
  const complete = (index: number, id: string) => act(async () => uploads[index].finish(new Response(JSON.stringify({
    id, filename: "evidence.txt", size: 17, mime_type: "text/plain",
  }))));

  it("reuploads a ready A file for B and never sends A's id", async () => {
    const { input, send, sendMessage } = setup();
    fireEvent.change(input, { target: { value: "Use the whole input" } });
    pick();
    await complete(0, "A-file");
    switchTo(other.id);
    await waitFor(() => expect(uploads).toHaveLength(2));
    expect(uploads.map((upload) => upload.sessionId)).toEqual([session.id, other.id]);
    expect(send).toBeDisabled();
    fireEvent.click(send);
    expect(sendMessage).not.toHaveBeenCalled();
    await complete(1, "B-file");
    fireEvent.click(send);
    expect(sendMessage).toHaveBeenCalledExactlyOnceWith(other.id, "Use the whole input", ["B-file"], undefined);
  });

  it("ignores late uploads across A to B to A and removal", async () => {
    const { send, sendMessage } = setup();
    pick();
    switchTo(other.id);
    await waitFor(() => expect(uploads).toHaveLength(2));
    switchTo(session.id);
    await waitFor(() => expect(uploads).toHaveLength(3));
    await complete(0, "old-A");
    await complete(1, "old-B");
    expect(send).toBeDisabled();
    await complete(2, "current-A");
    fireEvent.click(send);
    expect(sendMessage).toHaveBeenCalledExactlyOnceWith(session.id, "", ["current-A"], undefined);
    pick();
    await waitFor(() => expect(uploads).toHaveLength(4));
    fireEvent.click(screen.getByRole("button", { name: "Remove evidence.txt" }));
    pick();
    await waitFor(() => expect(uploads).toHaveLength(5));
    await complete(3, "removed-file");
    expect(screen.getAllByRole("button", { name: "Remove evidence.txt" })).toHaveLength(1);
    expect(send).toBeDisabled();
    expect(sendMessage).toHaveBeenCalledTimes(1);
    await complete(4, "replacement-file");
    fireEvent.click(send);
    expect(sendMessage).toHaveBeenLastCalledWith(session.id, "", ["replacement-file"], undefined);
  });

  it("blocks partial sending after upload failure and retries the same file", async () => {
    const { input, send, sendMessage } = setup();
    fireEvent.change(input, { target: { value: "Required file" } });
    pick();
    await act(async () => uploads[0].finish(new Response("Upload rejected", { status: 400 })));
    expect(send).toBeDisabled();
    fireEvent.click(send);
    expect(sendMessage).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Retry evidence.txt" }));
    await waitFor(() => expect(uploads).toHaveLength(2));
    expect(uploads[1].file.name).toBe(uploads[0].file.name);
    await complete(1, "retried");
    fireEvent.click(send);
    expect(sendMessage).toHaveBeenCalledExactlyOnceWith(session.id, "Required file", ["retried"], undefined);
  });

  it("restores a refused draft by reuploading even in its original session", async () => {
    const { input, send } = setup(async () => ({ accepted: false, message: "attachment_unavailable" }));
    fireEvent.change(input, { target: { value: "Retain all input" } });
    pick();
    await complete(0, "stale-file");
    await act(async () => fireEvent.click(send));
    fireEvent.click(screen.getByRole("button", { name: "Restore draft" }));
    await waitFor(() => expect(uploads).toHaveLength(2));
    expect(input).toHaveValue("Retain all input");
    expect(send).toBeDisabled();
    await complete(1, "fresh-file");
    expect(send).not.toBeDisabled();
  });

  const recordQueuedFailure = () => act(() => useSessionStore.getState().addMessage(session.id, {
    role: "system", type: "result",
    content: { kind: "outcome", request_id: "accepted-files", status: "failed", code: "attachment_unavailable",
      message: "Some attachments are unavailable. Re-upload the files in this conversation.", recovery: {} },
    tool_input: { rejected_input: { content: "Queued complete input", attachment_ids: ["lost-file"] } },
  }));

  it("restores a queued-input failure using its original File objects", async () => {
    const { input, send, sendMessage } = setup(async () => ({ accepted: true, requestId: "accepted-files" }));
    fireEvent.change(input, { target: { value: "Queued complete input" } });
    pick();
    await complete(0, "lost-file");
    await act(async () => fireEvent.click(send));
    recordQueuedFailure();
    fireEvent.click(await screen.findByRole("button", { name: "Restore draft and re-upload files" }));
    await waitFor(() => expect(uploads).toHaveLength(2));
    expect(input).toHaveValue("Queued complete input");
    expect(send).toBeDisabled();
    await complete(1, "replacement-file");
    fireEvent.click(send);
    expect(sendMessage).toHaveBeenLastCalledWith(session.id, "Queued complete input", ["replacement-file"], undefined);
  });

  it("requires file reselection when restoring a durable failure after reload", async () => {
    const { input, send, sendMessage } = setup();
    recordQueuedFailure();
    fireEvent.click(await screen.findByRole("button", { name: "Restore draft and re-upload files" }));
    expect(input).toHaveValue("Queued complete input");
    expect(send).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("Re-attach 1 file");
    fireEvent.click(send);
    expect(sendMessage).not.toHaveBeenCalled();
    pick();
    await complete(0, "reselected-file");
    fireEvent.click(send);
    expect(sendMessage).toHaveBeenCalledExactlyOnceWith(session.id, "Queued complete input", ["reselected-file"], undefined);
  });
});

it("restores a server-saved input without sending, then explicitly sends a new recovery request", async () => {
  useSessionStore.setState({ token: "recoverable-input-test" });
  const execution = { state: "withdrawn", state_version: 2, execution_owner_request_id: "old-request", recoverable_input: true };
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.endsWith("/submissions")) return Response.json({ items: [{ client_request_id: "old-client", request_id: "old-request", execution,
      preview: "Recover this input", attachment_count: 0 }], next_cursor: null });
    if (url.endsWith("/submissions/old-client")) return Response.json({ session_id: session.id, client_request_id: "old-client", request_id: "old-request",
      accepted: true, execution, input: { content: "Recover this input", attachment_ids: [], attachments: [] } });
    return Response.json([]);
  }));
  const { input, send, sendMessage } = setup(async () => ({ accepted: true }));
  fireEvent.click(await screen.findByRole("button", { name: "Restore to input" }));
  await waitFor(() => expect(input).toHaveValue("Recover this input"));
  expect(sendMessage).not.toHaveBeenCalled();
  fireEvent.change(input, { target: { value: "Reviewed and edited input" } });
  fireEvent.click(send);
  expect(sendMessage).toHaveBeenCalledExactlyOnceWith(session.id, "Reviewed and edited input", undefined, undefined, undefined, "old-client");
  vi.unstubAllGlobals();
});
