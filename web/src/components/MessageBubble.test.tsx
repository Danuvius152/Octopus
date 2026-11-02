/**
 * Streaming text renders as plain text, not markdown.
 *
 * Re-parsing a growing string through remark-gfm + remark-math on every 50ms
 * flush is O(n²) work on a long answer and janks the main thread; half-written
 * markdown also renders broken while it arrives (an unclosed code fence, a lone
 * table row). The completed block that replaces it renders normally.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";

import { MessageBubble } from "./MessageBubble";
import { useSessionStore } from "../stores/sessionStore";

afterEach(cleanup);

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
      kind: "outcome", status, request_id: "r1", recovery: { transient: 1 },
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
