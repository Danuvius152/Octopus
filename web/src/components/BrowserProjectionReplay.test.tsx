import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import fixtures from "../lib/browserProjectionReplay.json";
import { decodeWsEvent } from "../lib/wsEvents";
import { handleWsMessage } from "../hooks/useWebSocket";
import { applyTranscript } from "../lib/transcript";
import { useSessionStore } from "../stores/sessionStore";
import { MessageBubble } from "./MessageBubble";
import { NativeSubagentDetails } from "./NativeSubagentDetails";

// Python verifies these actual public projections before normalizing long IDs.
const samples = fixtures as Record<string, { event: unknown; prefix?: unknown[]; full?: unknown; message?: unknown; native?: unknown }>;
beforeEach(() => useSessionStore.setState(useSessionStore.getInitialState(), true));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it.each(["recovery", "async_question", "missing_final"])("preserves %s semantics after real server minimization", (kind) => {
  const sample = samples[kind];
  expect(decodeWsEvent(sample.event).kind).toBe("event");
  applyTranscript("sample-session", { messages: sample.prefix, next_message_seq: sample.prefix!.length });
  handleWsMessage(sample.event);
  const messages = useSessionStore.getState().messages["sample-session"];
  const message = messages.at(-1)!;
  const view = render(<MessageBubble sessionId="sample-session" message={message} />);
  expect(view.getByRole("button", { name: "View full message" })).toBeVisible();
  if (kind === "recovery") expect(view.getByText("Recovering · attempt 1/2")).toBeVisible();
  else if (kind === "async_question") expect(view.getByRole("region", { name: "Question for you" })).toBeVisible();
  else {
    expect(message.outcome).toMatchObject({ final_answer: {
      thread_id: "native-root", turn_id: "turn", message_seqs: [0],
    }, recording_issues: { native_final_missing: 1 } });
    expect(messages[0]).toMatchObject({ seq: 0, role: "assistant", content: "Saved final candidate",
      runtime_metadata: { request_id: "request", attempt_id: "attempt", native: { phase: "final_answer" } } });
    expect(view.getByText(/A declared final answer is missing from the saved transcript\./)).toBeVisible();
    expect(view.queryByText("Completion details")).not.toBeInTheDocument();
    expect(view.queryByText("Model output was saved.")).not.toBeInTheDocument();
  }
});

it.each(["command_inline", "command_preview", "command_unavailable"].flatMap((kind) =>
  ["http", "ws", "native"].map((surface) => ({ kind, surface }))))(
  "hides only the injected command prefix through real $kind / $surface projections and full reads", async ({ kind, surface }) => {
    const sample = samples[kind];
    useSessionStore.setState({ token: "original-account" });
    const fetcher = vi.fn(async (url: string) => Response.json(url.includes("native-details") ? sample.native : sample.full));
    vi.stubGlobal("fetch", fetcher);
    let view;
    if (surface === "native") {
      view = render(<NativeSubagentDetails sessionId="sample-session" anchorSeq={0} />);
      await view.findByText(/Observed helper state/);
    } else {
      if (surface === "http") applyTranscript("sample-session", { messages: [sample.message], next_message_seq: 2 });
      else handleWsMessage(sample.event);
      const message = useSessionStore.getState().messages["sample-session"].at(-1)!;
      view = render(<MessageBubble sessionId="sample-session" message={message} />);
      expect(view.container).not.toHaveTextContent("[输出已缩短：");
      const header = view.container.querySelector<HTMLButtonElement>(".tool-header");
      if (header) fireEvent.click(header);
    }
    expect(view.container).not.toHaveTextContent("[输出已缩短：");
    if (kind !== "command_inline") {
      // Normal tool expansion already starts the read. Native details and
      // identity notices expose an explicit saved-record button instead.
      if (surface === "native" || kind === "command_unavailable") {
        fireEvent.click(await view.findByRole("button", { name: "View full message" }));
      }
      await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/sessions/sample-session/messages/1?view=browser", expect.anything()));
    }
    await waitFor(() => expect(view.container).toHaveTextContent("HEAD"));
    expect(view.container).toHaveTextContent("TAIL");
    expect(view.container).toHaveTextContent("[... 中段已省略 ...]");
    expect(view.container).not.toHaveTextContent("[输出已缩短：");
  },
);

it.each(["unavailable_question", "unavailable_small_question"])("expands %s from a real WS notice into a complete readonly question record", async (kind) => {
  const sample = samples[kind];
  expect(decodeWsEvent(sample.event).kind).toBe("event");
  useSessionStore.setState({ token: "original-account" });
  handleWsMessage(sample.event);
  const message = useSessionStore.getState().messages["sample-session"][0];
  const live = useSessionStore.getState().subagents;
  vi.stubGlobal("fetch", vi.fn(async () => Response.json(sample.full)));
  const view = render(<MessageBubble sessionId="sample-session" message={message} />);
  fireEvent.click(view.getByRole("button", { name: "View full message" }));
  await waitFor(() => expect(view.getByText(/native-root/)).toBeVisible());
  expect(view.getByText(/Original unavailable option/)).toBeVisible();
  expect(view.queryByRole("button", { name: "Submit" })).not.toBeInTheDocument();
  expect(useSessionStore.getState().subagents).toBe(live);
});
