/**
 * Streaming text renders as plain text, not markdown.
 *
 * Re-parsing a growing string through remark-gfm + remark-math on every 50ms
 * flush is O(n²) work on a long answer and janks the main thread; half-written
 * markdown also renders broken while it arrives (an unclosed code fence, a lone
 * table row). The completed block that replaces it renders normally.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";

import { MessageBubble } from "./MessageBubble";
import { useSessionStore } from "../stores/sessionStore";

afterEach(cleanup);

const nativeRuntime = (item_id: string) => ({ request_id: "request", attempt_id: "attempt", native: {
  thread_id: "root", turn_id: "turn", root_thread_id: "root", root_turn_id: "turn", item_id } });

it("shows a Bash command once without rendering native bookkeeping", () => {
  const command = "/bin/bash -lc 'cp source/trace.zip workspace/trace.zip'";
  const input = { command, native_item: { type: "commandExecution", id: "exec-original", command,
    cwd: "/working", status: "inProgress", processId: "123", exitCode: null,
    commandActions: [{ type: "unknown", command }] } };
  const view = render(<MessageBubble sessionId="bash-reader" message={{ role: "assistant", type: "tool_use",
    tool_name: "Bash", tool_input: input, runtime_metadata: nativeRuntime("exec-original") }} />);
  fireEvent.click(view.container.querySelector<HTMLButtonElement>(".tool-header")!);
  const detail = view.container.querySelector(".tool-detail")!;
  expect(detail.textContent).toBe(command);
  expect(detail).not.toHaveTextContent("native_item");
  expect(view.queryByText("Raw tool data", { exact: true })).not.toBeInTheDocument();
  expect(view.queryByLabelText("Raw tool input")).not.toBeInTheDocument();
  expect(view.container).not.toHaveTextContent("exec-original");
  expect(input.native_item.commandActions).toEqual([{ type: "unknown", command }]);
});

it("preserves structured Bash input when no canonical command is available", () => {
  const view = render(<MessageBubble sessionId="bash-reader" message={{ role: "assistant", type: "tool_use",
    tool_name: "Bash", tool_input: { unknown_argument: "original input" } }} />);
  fireEvent.click(view.container.querySelector<HTMLButtonElement>(".tool-header")!);
  expect(view.container.querySelector(".tool-detail")).toHaveTextContent('"unknown_argument": "original input"');
});

it("keeps real Bash options alongside the command", () => {
  const parameters = { command: "echo original", timeout: 5000, description: "Check the original file" };
  const view = render(<MessageBubble sessionId="tool-reader" message={{ role: "assistant", type: "tool_use",
    tool_name: "Bash", tool_input: parameters }} />);
  fireEvent.click(view.container.querySelector<HTMLButtonElement>(".tool-header")!);
  expect(view.container.querySelector(".tool-detail")!.textContent).toBe(JSON.stringify(parameters, null, 2));
});

it.each([
  { native_item: { type: "futureNativeTool", id: "native-id", meaningful_field: "Original business value", extension: null } },
  { threadId: "thread", turnId: "turn", item: { type: "fileChange", id: "native-id", changes: [{ path: "/original", diff: "+saved change" }] },
    unknown_sibling: { business: "Keep the original observation" } },
])("keeps native-only or unknown input readable when no separate arguments exist", (tool_input) => {
  const view = render(<MessageBubble sessionId="tool-reader" message={{ role: "assistant", type: "tool_use",
    tool_name: "nativeTool", tool_input, runtime_metadata: nativeRuntime("native-id") }} />);
  fireEvent.click(view.container.querySelector<HTMLButtonElement>(".tool-header")!);
  expect(view.container.querySelector(".tool-detail")!.textContent).toBe(JSON.stringify(tool_input, null, 2));
});

it.each(["Read", "mcp__wiki__read", "mcp__custom__lookup"])("separates %s native bookkeeping while retaining all business arguments", (tool_name) => {
  const parameters = { path: "/original", id: "business-id", nullable: null, options: { input: "real argument" },
    request_context: { request_id: "business-request", content: "Original business context" }, attempt_context: null };
  const input = { ...parameters, native_item: { type: "mcpToolCall", id: "native-audit-id", arguments: parameters, status: "completed" } };
  const view = render(<MessageBubble sessionId="tool-reader" message={{ role: "assistant", type: "tool_use", tool_name, tool_input: input,
    runtime_metadata: nativeRuntime("native-audit-id") }} />);
  fireEvent.click(view.container.querySelector<HTMLButtonElement>(".tool-header")!);
  expect(view.container.querySelector(".tool-detail")!.textContent).toBe(JSON.stringify(parameters, null, 2));
  expect(view.queryByText("Raw tool data", { exact: true })).not.toBeInTheDocument();
  expect(view.queryByLabelText("Raw tool input")).not.toBeInTheDocument();
  expect(input.native_item.arguments).toBe(parameters);
});

it.each([undefined, nativeRuntime("different-item")])("preserves envelope-like business arguments without matching native provenance", (runtime_metadata) => {
  const input = { id: "business-id", input: null, request_context: { caller: "business caller" }, attempt_context: { mode: "business" },
    native_item: { id: "business-item", type: "custom", value: "Actual argument" } };
  const view = render(<MessageBubble sessionId="tool-reader" message={{ role: "assistant", type: "tool_use",
    tool_name: "mcp__custom__lookup", tool_input: input, runtime_metadata }} />);
  fireEvent.click(view.container.querySelector<HTMLButtonElement>(".tool-header")!);
  expect(view.container.querySelector(".tool-detail")!.textContent).toBe(JSON.stringify(input, null, 2));
  expect(view.queryByText("Raw tool data", { exact: true })).not.toBeInTheDocument();
});

it("keeps an asynchronous question visible when its saved body needs expansion", () => {
  const view = render(<MessageBubble sessionId="question-preview" message={{ role: "assistant", type: "text", seq: 4,
    content: "Question body preview…", body_state: "preview", detail_ref: "/api/sessions/question-preview/messages/4",
    tool_input: { async_questions_preview: true } }} />);
  expect(view.getByRole("region", { name: "Question for you" })).toBeVisible();
  expect(view.getByRole("button", { name: "View full message" })).toBeVisible();
});

describe("saved message previews", () => {
  afterEach(() => vi.unstubAllGlobals());
  const preview = { role: "assistant" as const, type: "text", seq: 7, content: "Saved preview…",
    body_state: "preview" as const, detail_ref: "/api/sessions/original/messages/7" };

  it("loads only on expansion, retains preview on failure and retries the full GET", async () => {
    useSessionStore.setState({ token: "preview-token" });
    const fetcher = vi.fn().mockResolvedValueOnce(new Response("missing", { status: 503 }))
      .mockResolvedValueOnce(Response.json({ role: "assistant", type: "text", seq: 7, content: "Complete original answer" }));
    vi.stubGlobal("fetch", fetcher);
    const view = render(<MessageBubble sessionId="original" message={preview} />);
    expect(fetcher).not.toHaveBeenCalled();
    fireEvent.click(view.getByRole("button", { name: "View full message" }));
    await waitFor(() => expect(view.getByRole("alert")).toHaveTextContent("503"));
    expect(view.getByText("Saved preview…")).toBeVisible();
    fireEvent.click(view.getByRole("button", { name: "Retry full message" }));
    await waitFor(() => expect(view.getByText("Complete original answer")).toBeVisible());
    expect(view.getByRole("button", { name: "Show preview" })).toBeVisible();
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([`${preview.detail_ref}?view=browser`, `${preview.detail_ref}?view=browser`]);
  });

  it.each(["tool_use", "tool_result"])("loads a %s through its existing expansion without preview notices", async (type) => {
    useSessionStore.setState({ token: "tool-reader" });
    const tool = { ...preview, type, tool_name: "Read", tool_input: { file_path: "/original" }, content: "Short result" };
    const fetcher = vi.fn().mockResolvedValue(Response.json({ ...tool, content: "Complete result", tool_input: { file_path: "/original", complete: true } }));
    vi.stubGlobal("fetch", fetcher);
    const view = render(<MessageBubble sessionId="original" message={tool} />);
    expect(view.queryByText("Preview · some content is not shown")).not.toBeInTheDocument();
    expect(view.queryByRole("button", { name: "View full message" })).not.toBeInTheDocument();
    expect(fetcher).not.toHaveBeenCalled();
    fireEvent.click(view.container.querySelector<HTMLButtonElement>(".tool-header")!);
    await waitFor(() => expect(view.getByText(type === "tool_use" ? /"complete": true/ : "Complete result")).toBeVisible());
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([`${preview.detail_ref}?view=browser`]);
    fireEvent.click(view.container.querySelector<HTMLButtonElement>(".tool-header")!);
    fireEvent.click(view.container.querySelector<HTMLButtonElement>(".tool-header")!);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("renders the complete saved card record after expansion without replacing live card state", async () => {
    const card = { role: "system" as const, type: "native_subagent", seq: 7, body_state: "preview" as const,
      detail_ref: "/api/sessions/original/messages/7", tool_input: { card_id: "card", task_id: "task", status: "failed", name: "Preview helper" } };
    useSessionStore.setState({ token: "card-token", messages: { original: [card] }, subagents: {} });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ role: "system", type: "native_subagent", seq: 7,
      runtime_metadata: { request_id: "complete-saved-request", attempt_id: "complete-saved-attempt" },
      tool_input: { ...card.tool_input, name: "Complete saved helper name", description: "Complete saved card description", steps: ["Complete saved step"] } })));
    const view = render(<MessageBubble sessionId="original" message={card} />);
    fireEvent.click(view.getByRole("button", { name: "View full message" }));
    await waitFor(() => expect(view.getByText(/Complete saved card description/)).toBeVisible());
    expect(view.getByText(/Complete saved step/)).toBeVisible();
    expect(view.getByText(/complete-saved-request/)).toBeVisible();
    expect(useSessionStore.getState().messages.original[0].tool_input?.name).toBe("Preview helper");
  });

  it("exposes original identity fields when an unavailable association is read in full", async () => {
    useSessionStore.setState({ token: "identity-reader" });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ role: "assistant", type: "question_request", seq: 7,
      runtime_metadata: { request_id: "original-request", attempt_id: "original-attempt", native: {
        thread_id: "Original native identity", turn_id: "turn", root_thread_id: "root", root_turn_id: "turn" } },
      tool_input: { questions: [{ question: "Original question", options: [{ label: "Original saved option" }] }] } })));
    const view = render(<MessageBubble sessionId="original" message={{ ...preview, type: "notice",
      content: "Original associations are unavailable in this preview.", identity_unavailable: true }} />);
    fireEvent.click(view.getByRole("button", { name: "View full message" }));
    await waitFor(() => expect(view.getByText(/Original native identity/)).toBeVisible());
    expect(view.getByText(/Original saved option/)).toBeVisible();
    expect(view.queryByRole("button", { name: "Submit" })).not.toBeInTheDocument();
  });

  it.each(["message", "account", "window"])("does not publish an unfinished or retired read across %s changes", async (boundary) => {
    useSessionStore.setState({ token: "preview-token" });
    let finish!: (value: unknown) => void;
    const body = new Promise((resolve) => { finish = resolve; });
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: () => body });
    vi.stubGlobal("fetch", fetcher);
    const view = render(<MessageBubble sessionId="original" message={preview} />);
    fireEvent.click(view.getByRole("button", { name: "View full message" }));
    await waitFor(() => expect(view.getByText("Loading full message…")).toBeVisible());
    expect(view.queryByText("Full message")).not.toBeInTheDocument();
    if (boundary === "message") view.rerender(<MessageBubble sessionId="next" message={{ ...preview, seq: 8, content: "Next preview…",
      detail_ref: "/api/sessions/next/messages/8" }} />);
    else act(() => {
      if (boundary === "account") useSessionStore.setState({ accountGeneration: useSessionStore.getState().accountGeneration + 1 });
      else useSessionStore.getState().setMessages("original", []);
    });
    expect(fetcher.mock.calls[0][1].signal.aborted).toBe(true);
    finish({ role: "assistant", type: "text", seq: 7, content: "Retired original answer" });
    await waitFor(() => expect(view.getByText(boundary === "message" ? "Next preview…" : "Saved preview…")).toBeVisible());
    expect(view.queryByText("Retired original answer")).not.toBeInTheDocument();
  });
});

it.each(["missing", "other-request", "other-attempt", "commentary", "other-root"])(
  "does not claim saved output for an unconfirmed %s reference", (damage) => {
    const metadata = { request_id: damage === "other-request" ? "other" : "r", attempt_id: damage === "other-attempt" ? "other" : "a",
      native: { thread_id: damage === "other-root" ? "other" : "root", turn_id: "turn", root_thread_id: "root", root_turn_id: "turn",
        item_id: "final", phase: damage === "commentary" ? "commentary" : "final_answer" } };
    useSessionStore.setState({ messages: { evidence: damage === "missing" ? [] : [{ seq: 1, role: "assistant", type: "text",
      content: "Collected text", runtime_metadata: metadata }] } });
    const view = render(<MessageBubble sessionId="evidence" message={{ role: "system", type: "result", seq: 2,
      tool_input: { attempt_context: { request_id: "r", attempt_id: "a" } }, outcome: {
        request_id: "r", status: "success", native_status: "completed", recovery: {},
        final_answer: { thread_id: "root", turn_id: "turn", message_seqs: [1] },
      } }} />);
    fireEvent.click(view.getByText("Completion details"));
    expect(view.queryByText("Model output was saved.")).not.toBeInTheDocument();
  },
);

it.each(["live", "history"])("keeps compact effect separate from an interrupted business outcome in %s records", async (path) => {
  const { handleWsMessage } = await import("../hooks/useWebSocket");
  const { normalizeMessage } = await import("../lib/turnOutcome");
  const content = { kind: "outcome", operation: "compact", request_id: "compact-original", status: "interrupted",
    recovery: {}, compact: { effect: "applied", item_ids: ["native-compact-item"] }, final_answer: null };
  useSessionStore.setState({ messages: {}, lastAppliedSeq: {} });
  handleWsMessage({ type: "result", session_id: "compact", seq: 2, terminal: true, is_error: true, content });
  const message = path === "live" ? useSessionStore.getState().messages.compact[0] :
    normalizeMessage({ role: "system", type: "result", seq: 2, is_error: true, content });
  const view = render(<MessageBubble sessionId="compact" message={message} />);
  expect(view.getByText("Compaction interrupted")).toBeVisible();
  expect(view.getByText("Native history was compacted. Chat messages are preserved.")).toBeVisible();
  expect(view.queryByText("Model output was saved.")).not.toBeInTheDocument();
});

it.each(["live", "history"])("preserves native compact command details in %s results", async (path) => {
  const { handleWsMessage } = await import("../hooks/useWebSocket");
  const { normalizeMessage } = await import("../lib/turnOutcome");
  const content = { kind: "outcome", operation: "compact", request_id: "compact-command", status: "failed", recovery: {},
    message: "Error: No messages to compact", compact: { effect: "unknown", item_ids: [], native_command: {
      input_uuid: "original-native-input", local_command_run: { command: "compact", args: "" },
      local_command_outcome: { kind: "failed" },
    } } };
  useSessionStore.setState({ messages: {}, lastAppliedSeq: {} });
  handleWsMessage({ type: "result", session_id: "native-command", seq: 1, terminal: true, is_error: true, content });
  const message = path === "live" ? useSessionStore.getState().messages["native-command"][0] :
    normalizeMessage({ role: "system", type: "result", seq: 1, is_error: true, content });
  const view = render(<MessageBubble sessionId="native-command" message={message} />);
  expect(view.getByText(/Error: No messages to compact/)).toBeVisible();
  fireEvent.click(view.getByText("Completion details"));
  const details = view.getByLabelText("Native command details");
  expect(details).toBeVisible();
  expect(details).toHaveTextContent('"input_uuid": "original-native-input"');
  expect(details).toHaveTextContent('"command": "compact"');
  expect(details).toHaveTextContent('"kind": "failed"');
});

it.each([
  ["live", "failed", "unknown", "local"], ["history", "failed", "unknown", "local"],
  ["live", "interrupted", "unknown", "local"], ["history", "interrupted", "unknown", "local"],
  ["live", "failed", "applied", "local"], ["history", "failed", "applied", "local"],
  ["live", "interrupted", "applied", "local"], ["history", "interrupted", "applied", "local"],
  ["live", "failed", "unknown", "terminal-errors"], ["history", "failed", "unknown", "terminal-errors"],
  ["live", "failed", "applied", "terminal-errors"], ["history", "failed", "applied", "terminal-errors"],
])("shows the native compact reason in %s %s results with %s effects from %s", async (path, status, effect, source) => {
  const { handleWsMessage } = await import("../hooks/useWebSocket");
  const { normalizeMessage } = await import("../lib/turnOutcome");
  const content = { kind: "outcome", operation: "compact", request_id: "lost-terminal", status, recovery: {},
    message: status === "interrupted" ? "Stopped by user" : "Backend ended without a terminal result",
    compact: { effect, reason: source === "terminal-errors"
      ? "Native hook refused compaction\nNative command cannot continue" : "Native hook refused compaction", item_ids: [],
    native_command: source === "terminal-errors" ? undefined : {
      input_uuid: "original-native-input", local_command_outcome: { kind: "failed" },
    } } };
  useSessionStore.setState({ messages: {}, lastAppliedSeq: {} });
  handleWsMessage({ type: "result", session_id: "lost-terminal", seq: 1, terminal: true, is_error: true, content });
  const message = path === "live" ? useSessionStore.getState().messages["lost-terminal"][0] :
    normalizeMessage({ role: "system", type: "result", seq: 1, is_error: true, content });
  const view = render(<MessageBubble sessionId="lost-terminal" message={message} />);
  expect(view.getByText(/Native hook refused compaction/)).toBeVisible();
  if (source === "terminal-errors") expect(view.getByText(/Native command cannot continue/)).toBeVisible();
  expect(view.getByText(effect === "applied" ? /Native history was compacted/ : /Native history changes could not be confirmed/)).toBeVisible();
});

it("uses a neutral explanation for an unknown historical question closure", () => {
  const view = render(<MessageBubble sessionId="legacy-question" message={{ role: "system", type: "question_closed",
    content: "Legacy transport detail", tool_input: { reason: "unknown_legacy_reason" } }} />);
  expect(view.getByText("The original question is closed.")).toBeVisible();
  expect(view.getByText("Legacy transport detail")).not.toBeVisible();
  fireEvent.click(view.getByText("Original closure details"));
  expect(view.getByText("Legacy transport detail")).toBeVisible();
});

it.each(["live", "history"])("shows one canonical primary cause and keeps saved-output evidence in %s details", async (path) => {
  const { handleWsMessage } = await import("../hooks/useWebSocket");
  const { normalizeMessage } = await import("../lib/turnOutcome");
  const cause = "The model turn completed, but required tool results were not confirmed.";
  const content = { kind: "outcome", operation: "message", request_id: "primary", status: "failed", message: cause,
    native_status: "completed", recovery: {}, recording_integrity: "incomplete",
    recording_issues: { native_collection_incomplete: 1 }, recording_locations: [{ kind: "native_collection_incomplete", line: 7 }],
    final_answer: { thread_id: "root", turn_id: "turn", message_seqs: [1] } };
  const tool_input = { attempt_context: { request_id: "primary", attempt_id: "attempt" } };
  useSessionStore.setState({ messages: { primary: [{ role: "assistant", type: "text", seq: 1, content: "Saved final", runtime_metadata: {
    request_id: "primary", attempt_id: "attempt", native: { thread_id: "root", turn_id: "turn", root_thread_id: "root", root_turn_id: "turn",
      item_id: "final", phase: "final_answer" },
  } }] }, lastAppliedSeq: {} });
  handleWsMessage({ type: "result", session_id: "primary", seq: 2, terminal: true, is_error: true, content, tool_input });
  const message = path === "live" ? useSessionStore.getState().messages.primary[1] :
    normalizeMessage({ role: "system", type: "result", seq: 2, is_error: true, content, tool_input });
  expect(message.outcome?.recording_locations).toEqual(content.recording_locations);
  const view = render(<MessageBubble sessionId="primary" message={message} />);
  expect(view.getAllByText(cause)).toHaveLength(1);
  expect(view.getByText(cause)).toBeVisible();
  expect(view.queryByText("The answer was saved; completion records are incomplete.")).not.toBeInTheDocument();
  expect(view.getByText("Native: completed")).not.toBeVisible();
  fireEvent.click(view.getByText("Completion details"));
  expect(view.getByText("Native: completed")).toBeVisible();
  expect(view.getByText("Model output was saved.")).toBeVisible();
  expect(view.queryByText("Done")).not.toBeInTheDocument();
});

it.each(["live", "history"])("keeps native status and missing-final evidence in %s results", async (path) => {
  const { handleWsMessage } = await import("../hooks/useWebSocket");
  const { normalizeMessage } = await import("../lib/turnOutcome");
  const content = { kind: "outcome", operation: "message", request_id: "r", status: "failed", recovery: {}, native_status: "completed",
    recording_integrity: "incomplete", recording_issues: { native_final_missing: 1 },
    final_answer: { thread_id: "root", turn_id: "turn", message_seqs: [1] } };
  useSessionStore.setState({ messages: {}, lastAppliedSeq: {} });
  handleWsMessage({ type: "result", session_id: "integrity", terminal: true, seq: 2, content });
  const message = path === "live" ? useSessionStore.getState().messages.integrity[0] :
    normalizeMessage({ type: "result", role: "system", content });
  expect(message.outcome).toMatchObject({ recording_integrity: "incomplete", recording_issues: { native_final_missing: 1 } });
  const { getByText } = render(<MessageBubble sessionId="integrity" message={message} />);
  expect(getByText("Native: completed")).toBeTruthy();
  expect(getByText("A declared final answer is missing from the saved transcript.")).toBeTruthy();
});

it.each([["expired", "system", "System · expired"], ["answered", "agent", "Agent"],
  ["answered", "account", "You"], ["answered", "unknown", "Answer · source unknown"]])(
  "labels %s answers from %s", (disposition, kind, label) => {
    const { getByText, queryByText } = render(<MessageBubble sessionId="question-source" message={{
      type: "question_answer", role: "user", content: "Answer content",
      tool_input: { question_resolution: { disposition, source: { kind } } },
    }} />);
    expect(getByText(label)).toBeTruthy();
    if (kind !== "account") expect(queryByText("You")).toBeNull();
  },
);

it("offers a native image's original path through live WS and saved history", async () => {
  const { handleWsMessage } = await import("../hooks/useWebSocket");
  const savedPath = "/original/native-image.png";
  const metadata = { request_id: "r", attempt_id: "a", native: { thread_id: "root", turn_id: "turn",
    root_thread_id: "root", root_turn_id: "turn", item_id: "image" } };
  useSessionStore.setState({ messages: {}, lastAppliedSeq: {} });
  handleWsMessage({ type: "tool_result", session_id: "image-session", seq: 1, tool_use_id: "image",
    output: `Generated image saved at: ${savedPath}`, is_error: false, runtime_metadata: metadata,
    tool_input: { native_item: { id: "image", type: "imageGeneration", status: "completed", savedPath } } });
  const message = useSessionStore.getState().messages["image-session"][0];
  expect(message.tool_input?.native_item).toMatchObject({ savedPath });
  const writeText = vi.fn().mockResolvedValue(undefined);
  const descriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  try {
    const rendered = render(<MessageBubble sessionId="image-session" message={message} />);
    fireEvent.click(rendered.getByRole("button", { name: "Copy path" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(savedPath));
    fireEvent.click(rendered.getByRole("button", { name: "Open in viewer" }));
    expect(useSessionStore.getState().viewer).toEqual({ sessionId: "image-session", path: savedPath });
    rendered.unmount();
    const restored = render(<MessageBubble sessionId="image-session" message={JSON.parse(JSON.stringify(message))} />);
    expect(restored.getByRole("button", { name: "Copy path" })).toBeVisible();
  } finally {
    if (descriptor) Object.defineProperty(navigator, "clipboard", descriptor);
    else Reflect.deleteProperty(navigator, "clipboard");
  }
});

it("shows hook provenance as a collapsed tool observation", () => {
  const { getByRole, queryByText, getByText } = render(<MessageBubble sessionId="hook" message={{
    role: "tool", type: "tool_result", content: "Hook context from hook-1", tool_input: {
      native_item: { type: "hookPrompt", fragments: [{ text: "Hook context", hookRunId: "hook-1" }] },
    }, runtime_metadata: { request_id: "r", attempt_id: "a", native: {
      thread_id: "root", turn_id: "turn", root_thread_id: "root", root_turn_id: "turn" } },
  }} />);
  expect(queryByText("You")).toBeNull();
  fireEvent.click(getByRole("button", { name: /Hook prompt/ }));
  expect(getByText("Hook context from hook-1")).toBeVisible();
});

it.each(["old-tool", undefined])("keeps historical tool approvals readable without actions (id: %s)", (toolUseId) => {
  const { getByText, queryByRole } = render(<MessageBubble sessionId="approval-history" message={{
    role: "tool", type: "tool_approval_request", tool_name: "Write", tool_use_id: toolUseId,
    tool_input: { file_path: "/tmp/notes.txt", content: "Historical content" },
  }} />);
  expect(getByText("Write")).toBeTruthy();
  expect(getByText(/Historical tool approval request/)).toBeTruthy();
  expect(getByText(/Historical content/)).toHaveTextContent('"file_path": "/tmp/notes.txt"');
  expect(queryByRole("button")).toBeNull();
});

it("renders a persisted material-only delivery notice after history reload", () => {
  const content = "Task result retained; no automatic reply was started.";
  const { getByText } = render(<MessageBubble sessionId="retained" message={{
    role: "system", type: "delivery_notice", content,
  }} />);
  expect(getByText(content)).toBeTruthy();
});

it("keeps a native follow-up card separate from the same helper's earlier result", () => {
  const sessionId = "native-follow-up";
  useSessionStore.getState().setSubagents(sessionId, [
    {
      task_id: "child-thread", tool_call_id: "spawn-call", tool_use_id: "raw-spawn",
      status: "completed", name: "Auditor", description: "Review complete",
      prompt: "First review", summary: "Earlier verified answer", steps: [],
    },
    {
      task_id: "child-thread", tool_call_id: "follow-up-call", tool_use_id: "native-follow-up",
      status: "interrupted", stop_reason: "parent_done", name: "Auditor",
      description: "Reviewing changed file", prompt: "Review the change", summary: "", steps: [],
    },
  ]);
  const { getByText, getAllByText, container } = render(<>
    <MessageBubble sessionId={sessionId} message={{
      role: "assistant", type: "tool_use", tool_name: "subagent:spawn_agent", tool_use_id: "raw-spawn",
      tool_input: { prompt: "First review" },
      runtime_metadata: { request_id: "request", attempt_id: "attempt", tool_call_id: "spawn-call" },
    }} />
    <MessageBubble sessionId={sessionId} message={{
      role: "assistant", type: "tool_use", tool_name: "subagent:followup_task", tool_use_id: "native-follow-up",
      tool_input: { native_thread_id: "child-thread", agent_path: "auditor" },
      runtime_metadata: { request_id: "request", attempt_id: "attempt", tool_call_id: "follow-up-call" },
    }} />
  </>);
  expect(getByText("subagent:followup_task")).toBeTruthy();
  expect(getAllByText("Earlier verified answer")).toHaveLength(1);
  expect(getByText("done")).toBeTruthy();
  expect(getByText("interrupted")).toBeTruthy();
  expect(getByText("Parent turn ended before completion")).toBeTruthy();
  expect(container.querySelectorAll(".subagent-card")).toHaveLength(2);
});

it("renders a failed result with its cause instead of Done", () => {
  const { getByText, queryByText } = render(
    <MessageBubble sessionId="s1" message={{
      role: "system", type: "result", is_error: true,
      content: "Selected model is at capacity",
    }} />
  );
  expect(getByText("Failed")).toBeTruthy();
  expect(getByText("Selected model is at capacity")).toBeTruthy();
  expect(queryByText(/Done/)).toBeNull();
});

it.each([
  ["success", "Done"], ["interrupted", "Interrupted"], ["unknown", "Ended · outcome not recorded"],
])("shows %s outcomes honestly after loading history", (status, label) => {
  useSessionStore.getState().setMessages("result-display", [{
    role: "system", type: "result",
    content: status === "unknown" ? null : {
      kind: "outcome", operation: "message", status, request_id: "r1", recovery: { transient: 1 },
      message: status === "interrupted" ? "(interrupted by user)" : null,
    },
  }]);
  const { getByText, queryByText } = render(<MessageBubble sessionId="result-display"
    message={useSessionStore.getState().messages["result-display"][0]} />);
  expect(getByText(label)).toBeTruthy();
  if (status !== "unknown") expect(getByText("1 recovery")).toBeTruthy();
  if (status !== "success") expect(queryByText(/^Done/)).toBeNull();
});

it("keeps recovery details expandable while showing its attempt count", () => {
  useSessionStore.getState().setMessages("recovery-display", [{
    role: "system", type: "recovery", content: {
      kind: "recovery", phase: "retrying", code: "transient_retry",
      message: "Provider temporarily unavailable", attempt: 1, maximum: 2,
    },
  }]);
  const { container, getByText } = render(<MessageBubble sessionId="recovery-display"
    message={useSessionStore.getState().messages["recovery-display"][0]} />);
  expect(getByText("Recovering · attempt 1/2")).toBeTruthy();
  expect(getByText("Provider temporarily unavailable")).toBeTruthy();
  expect(container.querySelector("details")?.hasAttribute("open")).toBe(false);
});

it.each(["[bg-task-result]", "[wiki-maintenance-result]"])("honors human metadata for a typed %s marker", (marker) => {
  const { container, getByText } = render(<MessageBubble sessionId="source-test" message={{
    role: "user", type: "text", content: `${marker} I typed this`,
    tool_input: { request_context: { request_id: "human-1", source: "human" } },
  }} />);
  expect(getByText(`${marker} I typed this`)).toBeTruthy();
  expect(container.querySelector(".msg-user")).toBeTruthy();
  expect(container.querySelector(".msg-bg-result")).toBeNull();
});

it("shows the Wiki return as an automatic notice beside its task cards", () => {
  const { getByText, queryByText, queryByTestId } = render(<MessageBubble sessionId="source-test" onFork={() => {}} message={{
    role: "user", type: "text", content: '[wiki-maintenance-result]\n{"job_id":"internal-reference"}', seq: 5,
    tool_input: { request_context: { request_id: "wiki-1", source: "wiki" } },
  }} />);
  expect(getByText("Wiki maintenance results received")).toBeTruthy();
  expect(queryByText(/internal-reference/)).toBeNull();
  expect(queryByTestId("fork-from-here")).toBeNull();
});

it("does not offer a human rewind target for a structured automatic result", () => {
  const { queryByTestId } = render(<MessageBubble sessionId="source-test" onFork={() => {}} message={{
    role: "user", type: "text", content: "Research completed", seq: 4,
    tool_input: { request_context: { request_id: "research-1", source: "research" } },
  }} />);
  expect(queryByTestId("fork-from-here")).toBeNull();
});

const streaming = "# Heading\n\n```js\nconst x = 1;";

describe("MessageBubble plain mode", () => {
  it("renders streaming text verbatim, without parsing markdown", () => {
    const { container } = render(
      <MessageBubble
        message={{ role: "assistant", type: "text", content: streaming }}
        sessionId="s1"
        plain
      />
    );
    const el = container.querySelector(".msg-streaming-text");
    expect(el).toBeTruthy();
    // The raw characters are on screen — no <h1>, no half-open <pre>.
    expect(el?.textContent).toBe(streaming);
    expect(container.querySelector("h1")).toBeNull();
  });

  it("renders the completed block as markdown", () => {
    const { container } = render(
      <MessageBubble
        message={{ role: "assistant", type: "text", content: "# Heading" }}
        sessionId="s1"
      />
    );
    expect(container.querySelector(".msg-streaming-text")).toBeNull();
    expect(container.querySelector("h1")?.textContent).toBe("Heading");
  });
});

describe("steered marker", () => {
  it("marks a message that was sent into the running turn", () => {
    const { container } = render(
      <MessageBubble
        message={{ role: "user", type: "text", content: "not that file", steered: true }}
        sessionId="s1"
      />
    );
    expect(container.querySelector(".msg-steered-marker")?.textContent).toContain(
      "sent to the running turn"
    );
  });

  it("leaves an ordinary user message unmarked", () => {
    // A queued message and a steered one look identical in the transcript but
    // behave very differently; only the steered one says so.
    const { container } = render(
      <MessageBubble
        message={{ role: "user", type: "text", content: "do this next" }}
        sessionId="s1"
      />
    );
    expect(container.querySelector(".msg-steered-marker")).toBeNull();
  });
});
