import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { VirtuosoMockContext } from "react-virtuoso";
import { ChatView } from "./ChatView";
import { mockChatViewport } from "./chatViewport.test-support";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { handleWsMessage } from "../hooks/useWebSocket";
import type { SubmissionReply } from "../lib/submission";
import { deferred } from "../test-support/deferred";
import { chatDraft } from "../lib/chatDraft";

const session = { id: "receipt-session", name: "Receipts", working_dir: "/tmp", created_at: "2026-11-17",
  status: "idle", backend: "codex" } as SessionInfo;
type Operation = "message" | "compact" | "continuation";

async function uploadedText(body: RequestInit["body"]): Promise<string> {
  const file = (body as FormData).get("file") as File;
  expect(file).toMatchObject({ name: "evidence.txt", size: 8, type: "text/plain" });
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(file);
  });
}

beforeEach(() => {
  mockChatViewport();
  useSessionStore.setState({ ...useSessionStore.getInitialState(),
    accountGeneration: useSessionStore.getState().accountGeneration + 1,
    activeSessionId: session.id, sessions: [session], token: "receipt-account" });
});

it("retires a completed command error on A → B → A without clearing the shared draft or unknown receipt", async () => {
  await prepare("message", { accepted: true, state: "unknown", clientRequestId: "original-client" });
  const input = screen.getByRole("textbox");
  fireEvent.change(input, { target: { value: "/compact invalid" } });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Send message" })));
  expect(screen.getByText("Usage: /compact (no arguments).")).toBeInTheDocument();
  fireEvent.change(input, { target: { value: "My later draft" } });
  act(() => useSessionStore.setState({ activeSessionId: "other", sessions: [session, { ...session, id: "other" }] }));
  expect(screen.queryByText("Usage: /compact (no arguments).")).not.toBeInTheDocument();
  act(() => useSessionStore.setState({ activeSessionId: session.id }));
  expect(screen.queryByText("Usage: /compact (no arguments).")).not.toBeInTheDocument();
  expect(input).toHaveValue("My later draft");
  expect(screen.getByRole("button", { name: "Retry admission confirmation" })).toBeEnabled();
});

it("does not replace a newer command error with an older operation's late response", async () => {
  await prepare("message", { accepted: true });
  const replies = [deferred<Response>(), deferred<Response>()];
  let posts = 0;
  vi.stubGlobal("fetch", vi.fn(async (_url: unknown, init?: RequestInit) => init?.method === "POST"
    ? replies[posts++].promise : Response.json({ items: [] })));
  for (let index = 0; index < 2; index++) {
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "/reset" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Send message" })));
  }
  await act(async () => replies[1].resolve(Response.json({ detail: "New reset refusal" }, { status: 409 })));
  expect(screen.getByText(/New reset refusal/)).toBeInTheDocument();
  await act(async () => replies[0].resolve(Response.json({ detail: "Old reset refusal" }, { status: 409 })));
  expect(screen.getByText(/New reset refusal/)).toBeInTheDocument();
  expect(screen.queryByText(/Old reset refusal/)).not.toBeInTheDocument();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

async function prepare(operation: Operation, reply: SubmissionReply, via: "direct" | "GET" = "direct",
  options: { edit?: boolean; file?: boolean; confirmation?: Promise<Response>; holdDelivery?: boolean } = {}) {
  const requests: { url: string; init?: RequestInit }[] = [];
  const delivery = deferred<SubmissionReply>();
  const initial = via === "GET" ? { accepted: false, state: "unknown" as const, clientRequestId: "original-client" } : reply;
  const fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    requests.push({ url: String(url), init });
    if (String(url).endsWith("/attachments")) {
      const file = (init!.body as FormData).get("file") as File;
      return Response.json({ id: "original-file", filename: file.name, size: file.size, mime_type: file.type });
    }
    if (init?.method === "POST") {
      const settled = await delivery.promise;
      const identity = { session_id: session.id, client_request_id: JSON.parse(String(init.body)).client_request_id };
      const receipt = { ...identity, accepted: settled.accepted, state: settled.state, message: settled.message, code: settled.code };
      return Response.json(settled.accepted || settled.execution ? {
        ...receipt, request_id: settled.requestId ?? "original-request", operation: "compact", execution: settled.execution,
      } : receipt);
    }
    if (String(url).includes("/submissions/")) return options.confirmation ?? Response.json({ ...reply,
      session_id: session.id, client_request_id: String(url).split("/").at(-1), request_id: "original-request",
      operation: operation === "compact" ? "compact" : "message" });
    return Response.json({ items: [], next_cursor: null });
  });
  vi.stubGlobal("fetch", fetch);
  const sendMessage = vi.fn<Parameters<typeof ChatView>[0]["sendMessage"]>(async (_sid, _content, _files, _wiki, id) =>
    ({ ...await delivery.promise, clientRequestId: id ?? "original-client" }));
  if (operation === "continuation") handleWsMessage({ type: "question_request", session_id: session.id,
    seq: 1, question_id: "original-question", request_id: "original-turn", attempt_id: "attempt",
    questions: [{ question: "Continue?", options: [] }], deadline: null });
  const view = render(<VirtuosoMockContext.Provider value={{ viewportHeight: 600, itemHeight: 80 }}>
    <ChatView sendMessage={sendMessage} interrupt={vi.fn()} answerQuestion={vi.fn()}
      connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />
  </VirtuosoMockContext.Provider>);
  if (operation === "continuation") {
    fireEvent.change(await screen.findByPlaceholderText("Or type your own answer…"), { target: { value: "Original decision" } });
    act(() => handleWsMessage({ type: "question_closed", session_id: session.id, seq: 2,
      question_id: "original-question", reason: "interrupted", message: "Question closed." }));
    fireEvent.click(await screen.findByRole("button", { name: "Continue as new message" }));
  } else fireEvent.change(screen.getByRole("textbox"), { target: { value: operation === "compact" ? "/compact" : "Original message" } });
  if (options.file) await act(async () => fireEvent.change(screen.getByTestId("attachment-file-input"), {
    target: { files: [new File(["evidence"], "evidence.txt", { type: "text/plain" })] },
  }));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Send message" })));
  if (options.edit) fireEvent.change(screen.getByRole("textbox"), { target: { value: "My later draft" } });
  if (!options.holdDelivery) await act(async () => delivery.resolve(initial));
  if (via === "GET") await act(async () => fireEvent.click(screen.getByRole("button", {
    name: operation === "continuation" ? "Check original receipt" : "Retry admission confirmation",
  })));
  return { sendMessage, requests, fetch, view, delivery };
}

it.each(["pending", "running", "done"] as const)("retains original Files after GET confirms message %s", async (state) => {
  const { sendMessage, requests } = await prepare("message", { accepted: true, requestId: "original-request",
    execution: { state, state_version: 1, execution_owner_request_id: "original-request", delivery_state: "none", recoverable_input: false } }, "GET", { file: true });
  fireEvent.click(screen.getByRole("button", { name: "Browse loaded content" }));
  act(() => useSessionStore.getState().addMessage(session.id, { role: "system", type: "result", seq: 7,
    content: { kind: "outcome", operation: "message", request_id: "original-request", status: "failed", code: "attachment_unavailable",
      message: "Re-upload the original files", recovery: {} },
    tool_input: { rejected_input: { content: "Original message", attachment_ids: ["original-file"] } } }));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Restore draft and re-upload files" })));
  const uploads = requests.filter(request => request.url.endsWith("/attachments"));
  expect(uploads).toHaveLength(2);
  expect(await uploadedText(uploads[1].init!.body)).toBe("evidence");
  expect(screen.queryByText(/Re-attach 1 file/)).toBeNull();
  expect(sendMessage).toHaveBeenCalledTimes(1);
});

it.each((["direct", "GET"] as const).flatMap(via =>
  (["before", "after"] as const).map(outcome => ({ via, outcome })),
))("retains background input Files when failure arrives $outcome its $via receipt", async ({ via, outcome }) => {
  const confirmation = deferred<Response>();
  const { sendMessage, requests, delivery } = await prepare("message", { accepted: true, requestId: "original-request" }, via,
    { file: true, holdDelivery: via === "direct", confirmation: confirmation.promise });
  act(() => useSessionStore.setState({ activeSessionId: "foreground-A", sessions: [session,
    { ...session, id: "foreground-A", name: "Foreground A" }] }));
  const failOriginal = () => act(() => useSessionStore.getState().addMessage(session.id, {
    role: "system", type: "result", seq: 7,
    content: { kind: "outcome", operation: "message", request_id: "original-request", status: "failed", code: "attachment_unavailable",
      message: "Re-upload the original files", recovery: {} },
    tool_input: { rejected_input: { content: "Original message", attachment_ids: ["original-file"] } },
  }));
  if (outcome === "before") failOriginal();
  if (via === "direct") await act(async () => delivery.resolve({ accepted: true, requestId: "original-request" }));
  else await act(async () => confirmation.resolve(Response.json({ accepted: true, session_id: session.id,
    client_request_id: sendMessage.mock.calls[0][4], request_id: "original-request", operation: "message",
    execution: { state: "done", state_version: 1, execution_owner_request_id: "original-request", delivery_state: "none", recoverable_input: false } })));
  if (outcome === "after") failOriginal();
  expect(screen.queryByText("Re-upload the original files")).toBeNull();
  expect(screen.queryByRole("button", { name: "Remove evidence.txt" })).toBeNull();
  act(() => useSessionStore.setState({ activeSessionId: session.id }));
  const browse = screen.queryByRole("button", { name: "Browse loaded content" });
  if (browse) fireEvent.click(browse);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Restore draft and re-upload files" })));
  const uploads = requests.filter(request => request.url.endsWith("/attachments"));
  expect(uploads).toHaveLength(2);
  expect(await uploadedText(uploads[1].init!.body)).toBe("evidence");
  expect(screen.getByRole("textbox")).toHaveValue("Original message");
  expect(screen.queryByText(/Re-attach 1 file/)).toBeNull();
  expect(sendMessage).toHaveBeenCalledOnce();
  expect(requests.filter(request => request.url.includes("/submissions/"))).toHaveLength(via === "GET" ? 1 : 0);
});

it.each((["message", "continuation"] as const).flatMap(operation =>
  (["direct", "GET"] as const).map(via => ({ operation, via })),
))("completes the original Files when $operation failure recovery precedes its $via receipt", async ({ operation, via }) => {
  const confirmation = deferred<Response>();
  const { sendMessage, requests, delivery } = await prepare(operation, { accepted: true }, via,
    { file: true, confirmation: confirmation.promise, holdDelivery: via === "direct" });
  const content = operation === "message" ? "Original message" : "Continuing a closed question (new message):\n\nQ: Continue?\nA: Original decision";
  fireEvent.click(screen.getByRole("button", { name: "Browse loaded content" }));
  act(() => useSessionStore.getState().addMessage(session.id, { role: "system", type: "result", seq: 7,
    content: { kind: "outcome", operation: "message", request_id: "original-request", status: "failed", code: "attachment_unavailable",
      message: "Re-upload the original files", recovery: {} },
    tool_input: { rejected_input: { content, attachment_ids: ["original-file"] } } }));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Restore draft and re-upload files" })));
  expect(screen.getByRole("textbox")).toHaveValue(content);
  expect(requests.filter(request => request.url.endsWith("/attachments"))).toHaveLength(1);
  expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  if (via === "direct") await act(async () => delivery.resolve({ accepted: true, requestId: "original-request" }));
  else await act(async () => confirmation.resolve(Response.json({ accepted: true,
    session_id: session.id, client_request_id: sendMessage.mock.calls[0][4], request_id: "original-request", operation: "message",
    execution: { state: "done", state_version: 2, execution_owner_request_id: "original-request", delivery_state: "none", recoverable_input: false } })));
  const uploads = requests.filter(request => request.url.endsWith("/attachments"));
  expect(uploads).toHaveLength(2);
  expect(await uploadedText(uploads[1].init!.body)).toBe("evidence");
  expect(screen.getByRole("button", { name: "Remove evidence.txt" })).toBeInTheDocument();
  expect(screen.queryByText(/Re-attach 1 file/)).toBeNull();
  expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled();
  expect(sendMessage).toHaveBeenCalledOnce();
  expect(requests.filter(request => request.url.includes("/submissions/"))).toHaveLength(via === "GET" ? 1 : 0);
});

it.each(["edit", "shared edit", "select file", "switch session", "switch account", "unmount"] as const)(
  "does not complete an abandoned File recovery after %s", async (change) => {
    const confirmation = deferred<Response>();
    const { sendMessage, requests, view } = await prepare("message", { accepted: true }, "GET",
      { file: true, confirmation: confirmation.promise });
    const clientId = sendMessage.mock.calls[0][4];
    fireEvent.click(screen.getByRole("button", { name: "Browse loaded content" }));
    act(() => useSessionStore.getState().addMessage(session.id, { role: "system", type: "result", seq: 7,
      content: { kind: "outcome", operation: "message", request_id: "original-request", status: "failed", code: "attachment_unavailable",
        message: "Re-upload the original files", recovery: {} },
      tool_input: { rejected_input: { content: "Original message", attachment_ids: ["original-file"] } } }));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Restore draft and re-upload files" })));
    let expectedText = "Original message";
    if (change === "edit") {
      fireEvent.change(screen.getByRole("textbox"), { target: { value: "My later draft" } });
      expectedText = "My later draft";
    } else if (change === "shared edit") {
      act(() => chatDraft().edit("An independently edited draft"));
      expectedText = "An independently edited draft";
    } else if (change === "select file") await act(async () => fireEvent.change(screen.getByTestId("attachment-file-input"), {
      target: { files: [new File(["later"], "later.txt", { type: "text/plain" })] },
    }));
    else if (change === "switch session") act(() => {
      useSessionStore.setState({ activeSessionId: "other", sessions: [session, { ...session, id: "other" }] });
      useSessionStore.setState({ activeSessionId: session.id });
    });
    else if (change === "switch account") {
      act(() => {
        useSessionStore.getState().setToken("new-account");
        useSessionStore.setState({ activeSessionId: session.id, sessions: [session] });
      });
      fireEvent.change(screen.getByRole("textbox"), { target: { value: "A new account draft" } });
      expectedText = "A new account draft";
    } else if (change === "unmount") {
      view.unmount();
      render(<ChatView sendMessage={vi.fn()} interrupt={vi.fn()} answerQuestion={vi.fn()}
        connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />);
    }
    await act(async () => confirmation.resolve(Response.json({ accepted: true,
      session_id: session.id, client_request_id: clientId, request_id: "original-request", operation: "message",
      execution: { state: "done", state_version: 2, execution_owner_request_id: "original-request", delivery_state: "none", recoverable_input: false } })));
    expect(screen.getByRole("textbox")).toHaveValue(expectedText);
    expect(requests.filter(request => request.url.endsWith("/attachments"))).toHaveLength(change === "select file" ? 2 : 1);
    expect(screen.queryByRole("button", { name: "Remove evidence.txt" })).toBeNull();
    if (change === "select file") expect(screen.getByRole("button", { name: "Remove later.txt" })).toBeInTheDocument();
    expect(sendMessage).toHaveBeenCalledOnce();
    expect(requests.filter(request => request.url.includes("/submissions/"))).toHaveLength(1);
  },
);

it.each(["direct", "GET"] as const)("hands accepted continuation Files to message recovery before settling its %s receipt", async (via) => {
  const { sendMessage, requests } = await prepare("continuation", { accepted: true, requestId: "original-request",
    execution: { state: "pending", state_version: 1, execution_owner_request_id: "original-request", delivery_state: "none", recoverable_input: false } }, via, { file: true });
  expect(screen.queryByRole("button", { name: "Continue as new message" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Browse loaded content" }));
  act(() => useSessionStore.getState().addMessage(session.id, { role: "system", type: "result", seq: 7,
    content: { kind: "outcome", operation: "message", request_id: "original-request", status: "failed", code: "attachment_unavailable",
      message: "Re-upload the original files", recovery: {} },
    tool_input: { rejected_input: { content: "Original decision", attachment_ids: ["original-file"] } } }));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Restore draft and re-upload files" })));
  const uploads = requests.filter(request => request.url.endsWith("/attachments"));
  expect(uploads).toHaveLength(2);
  expect(await uploadedText(uploads[1].init!.body)).toBe("evidence");
  expect(screen.queryByText(/Re-attach 1 file/)).toBeNull();
  expect(sendMessage).toHaveBeenCalledTimes(1);
});

it("keeps continuation text, Files and request ID through a wrong-operation GET", async () => {
  const { fetch, sendMessage, requests } = await prepare("continuation", { accepted: false, state: "unknown" }, "direct", { file: true });
  const original = Object.values(useSessionStore.getState().questionDrafts)[0];
  const id = sendMessage.mock.calls[0][4];
  const defaultFetch = fetch.getMockImplementation()!;
  let operation = "compact";
  fetch.mockImplementation(async (url, init) => String(url).endsWith(`/submissions/${id}`) ?
    Response.json({ session_id: session.id, client_request_id: id, accepted: true, operation, request_id: "confirmed-request",
      execution: { state: "pending", state_version: 1, execution_owner_request_id: "confirmed-request", delivery_state: "none", recoverable_input: false } }) :
    defaultFetch(url, init));

  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Check original receipt" })));
  const remaining = Object.values(useSessionStore.getState().questionDrafts)[0];
  expect(remaining).toBe(original);
  expect(remaining.transmission?.id).toBe(id);
  expect(remaining.transmission?.content).toContain("Original decision");
  expect(remaining.transmission?.attachments).toHaveLength(1);
  operation = "message";
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Check original receipt" })));
  expect(useSessionStore.getState().questionDrafts).toEqual({});
  fireEvent.click(screen.getByRole("button", { name: "Browse loaded content" }));
  act(() => useSessionStore.getState().addMessage(session.id, { role: "system", type: "result", seq: 7,
    content: { kind: "outcome", operation: "message", request_id: "confirmed-request", status: "failed", code: "attachment_unavailable",
      message: "Re-upload the original files", recovery: {} },
    tool_input: { rejected_input: { content: "Original decision", attachment_ids: ["original-file"] } } }));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Restore draft and re-upload files" })));
  const uploads = requests.filter((request) => request.url.endsWith("/attachments"));
  expect(uploads).toHaveLength(2);
  expect(await uploadedText(uploads[1].init!.body)).toBe("evidence");
  expect(sendMessage).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls.filter(([url]) => String(url).endsWith(`/submissions/${id}`))).toHaveLength(2);
});

it.each<Operation>(["message", "compact", "continuation"])("keeps accepted + unknown %s receipts for original-ID confirmation", async (operation) => {
  const { sendMessage, requests } = await prepare(operation, { accepted: true, clientRequestId: "original-client",
    execution: { state: "unknown", state_version: 1, execution_owner_request_id: "original-request",
      delivery_state: "unknown", recoverable_input: false } });
  if (operation === "continuation") {
    await waitFor(() => expect(screen.getByRole("button", { name: "Continue as new message" })).toBeDisabled());
    expect(Object.values(useSessionStore.getState().questionDrafts)[0].transmission?.state).toBe("unknown");
  } else {
    expect(await screen.findByRole("button", { name: "Retry admission confirmation" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Restore draft" })).toBeDisabled();
  }
  expect(operation === "compact" ? requests.filter(({ init }) => init?.method === "POST") : sendMessage.mock.calls).toHaveLength(1);
});

it("keeps an explicitly refused compact command as an unsent shared draft across remount", async () => {
  const { view } = await prepare("compact", { accepted: false, message: "Compaction refused before admission" });
  expect(screen.getByRole("textbox")).toHaveValue("/compact");
  view.unmount();
  render(<ChatView sendMessage={vi.fn()} interrupt={vi.fn()} answerQuestion={vi.fn()}
    connected onToggleSidebar={vi.fn()} onOpenSchedules={vi.fn()} />);
  expect(screen.getByRole("textbox")).toHaveValue("/compact");
});

it.each((["message", "compact", "continuation"] as const).flatMap((operation) =>
  (["direct", "GET"] as const).flatMap((via) => (["pending", "running", "done", "rejected"] as const)
    .map((state) => ({ operation, via, state }))),
))("settles $operation $state facts from $via without changing a later draft", async ({ operation, via, state }) => {
  const reply: SubmissionReply = { accepted: false, message: "Original operation refused",
    ...(state === "rejected" ? {} : { execution: { state, state_version: 1,
      execution_owner_request_id: "original-request", delivery_state: "none", recoverable_input: false } }) };
  const { sendMessage, requests } = await prepare(operation, reply, via, { edit: true });
  expect(screen.getByRole("textbox")).toHaveValue("My later draft");
  if (state === "rejected") {
    expect(screen.getByText("Original operation refused")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "" } });
    if (operation !== "compact") expect(screen.getByRole("button", {
      name: operation === "continuation" ? "Continue as new message" : "Restore draft",
    })).toBeEnabled();
  } else {
    expect(screen.queryByRole("button", { name: "Restore draft" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Continue as new message" })).not.toBeInTheDocument();
  }
  expect(screen.queryByRole("button", { name: "Retry admission confirmation" })).not.toBeInTheDocument();
  expect(operation === "compact" ? requests.filter(({ url, init }) => init?.method === "POST" && url.endsWith("/submissions")) : sendMessage.mock.calls).toHaveLength(1);
});

it.each((["message", "compact", "continuation"] as const).flatMap((operation) =>
  (["direct", "GET"] as const).map((via) => ({ operation, via })),
))("confirms unknown $operation from $via with GET against its original ID and keeps later edits", async ({ operation, via }) => {
  const { sendMessage, requests } = await prepare(operation, { accepted: true,
    execution: { state: "unknown", state_version: 1, execution_owner_request_id: "original-request",
      delivery_state: "unknown", recoverable_input: false } }, via, { edit: true, file: operation !== "compact" });
  const id = operation === "compact" ? JSON.parse(String(requests.find(({ url, init }) => init?.method === "POST" && url.endsWith("/submissions"))!.init!.body)).client_request_id
    : sendMessage.mock.calls[0][4];
  await act(async () => fireEvent.click(screen.getByRole("button", {
    name: operation === "continuation" ? "Check original receipt" : "Retry admission confirmation",
  })));
  expect(requests.filter(({ url }) => url.includes("/submissions/"))).toHaveLength(via === "GET" ? 2 : 1);
  expect(requests.filter(({ url }) => url.includes("/submissions/")).every(({ url }) => url.endsWith(`/${id}`))).toBe(true);
  expect(screen.getByRole("textbox")).toHaveValue("My later draft");
  if (operation !== "compact") {
    expect(sendMessage).toHaveBeenCalledOnce();
    expect(sendMessage.mock.calls[0][2]).toEqual(["original-file"]);
    expect(operation === "message" ? screen.getByRole("button", { name: "Download evidence.txt" }) : screen.getByText("evidence.txt")).toBeInTheDocument();
  } else expect(requests.filter(({ init }) => init?.method === "POST")).toHaveLength(1);
});

it.each((["message", "continuation"] as const).flatMap((operation) =>
  (["direct", "GET"] as const).map((via) => ({ operation, via })),
))("restores $operation $via withdrawn files and links only an explicit new recovery to the original", async ({ operation, via }) => {
  const { sendMessage } = await prepare(operation, { accepted: true,
    execution: { state: "withdrawn", state_version: 2, execution_owner_request_id: "original-request",
      delivery_state: "none", recoverable_input: true } }, via, { file: true, edit: true });
  const id = sendMessage.mock.calls[0][4];
  expect(screen.getByRole("textbox")).toHaveValue("My later draft");
  const restore = screen.getByRole("button", { name: operation === "message" ? "Restore draft" : "Continue as new message" });
  expect(restore).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "" } });
  await act(async () => fireEvent.click(restore));
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toContain(operation === "message" ? "Original message" : "Original decision");
  expect(screen.getByRole("button", { name: "Remove evidence.txt" })).toBeInTheDocument();
  expect(sendMessage).toHaveBeenCalledOnce();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Send message" })));
  expect(sendMessage).toHaveBeenCalledTimes(2);
  expect(sendMessage.mock.calls[1][2]).toEqual(["original-file"]);
  expect(sendMessage.mock.calls[1][4]).not.toBe(id);
  expect(sendMessage.mock.calls[1][5]).toBe(id);
});

it.each<Operation>(["message", "compact", "continuation"])("ignores a late confirmation from the previous account for %s", async (operation) => {
  await prepare(operation, { accepted: false, state: "unknown", clientRequestId: "original-client" });
  const receipt = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => String(url).includes("/submissions/")
    ? receipt.promise : Response.json({ items: [] })));
  await act(async () => fireEvent.click(screen.getByRole("button", {
    name: operation === "continuation" ? "Check original receipt" : "Retry admission confirmation",
  })));
  act(() => {
    useSessionStore.getState().setToken("next-account");
    useSessionStore.setState({ sessions: [session], activeSessionId: session.id });
    if (operation === "continuation") handleWsMessage({ type: "question_request", session_id: session.id, seq: 1,
      question_id: "new-question", request_id: "new-turn", attempt_id: "new-attempt",
      questions: [{ question: "New decision?", options: [] }], deadline: null });
  });
  if (operation === "continuation") {
    fireEvent.change(await screen.findByPlaceholderText("Or type your own answer…"), { target: { value: "New account answer" } });
    act(() => handleWsMessage({ type: "question_closed", session_id: session.id, seq: 2,
      question_id: "new-question", reason: "interrupted", message: "New question closed." }));
  }
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "New account draft" } });
  await act(async () => receipt.resolve(Response.json({ accepted: false, message: "Old account refusal" })));
  expect(screen.getByRole("textbox")).toHaveValue("New account draft");
  expect(screen.queryByText(/previous sign-in|Old account refusal|unconfirmed.*saved draft/i)).not.toBeInTheDocument();
});

it.each((["message", "compact", "continuation"] as const).flatMap((operation) =>
  (["direct", "GET"] as const).flatMap((via) => [false, true].map((recoverable) => ({ operation, via, recoverable }))),
))("keeps $operation withdrawn receipts $via read-only unless recovery is authorized ($recoverable)", async ({ operation, via, recoverable }) => {
  const { sendMessage, requests } = await prepare(operation, { accepted: true, clientRequestId: "original-client",
    execution: { state: "withdrawn", state_version: 2, execution_owner_request_id: "original-request",
      delivery_state: "none", recoverable_input: recoverable } }, via);
  const allowed = operation !== "compact" && recoverable;
  if (operation === "continuation") {
    const restore = screen.getByRole("button", { name: "Continue as new message" });
    expect(restore).toBeInTheDocument();
    expect(restore).toHaveProperty("disabled", !allowed);
  } else {
    if (operation === "compact") fireEvent.change(screen.getByRole("textbox"), { target: { value: "" } });
    const restore = screen.getByRole("button", { name: "Restore draft" });
    expect(restore).toHaveProperty("disabled", !allowed);
  }
  expect(operation === "compact" ? requests.filter(({ init }) => init?.method === "POST") : sendMessage.mock.calls).toHaveLength(1);
});
