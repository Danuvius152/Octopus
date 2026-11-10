import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import fixtures from "../lib/browserProjectionReplay.json";
import { decodeWsEvent } from "../lib/wsEvents";
import { handleWsMessage } from "../hooks/useWebSocket";
import { presentOutcome } from "../lib/turnOutcome";
import { applyTranscript } from "../lib/transcript";
import { useSessionStore } from "../stores/sessionStore";
import { MessageBubble } from "./MessageBubble";

// Python verifies these actual public projections before normalizing long IDs.
const samples = fixtures as Record<string, { event: unknown; prefix: unknown[]; full?: unknown }>;
beforeEach(() => useSessionStore.setState(useSessionStore.getInitialState(), true));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it.each(["recovery", "async_question", "missing_final"])("preserves %s semantics after real server minimization", (kind) => {
  const sample = samples[kind];
  expect(decodeWsEvent(sample.event).kind).toBe("event");
  applyTranscript("sample-session", { messages: sample.prefix, next_message_seq: sample.prefix.length });
  handleWsMessage(sample.event);
  const messages = useSessionStore.getState().messages["sample-session"];
  const message = messages.at(-1)!;
  const view = render(<MessageBubble sessionId="sample-session" message={message} />);
  expect(view.getByRole("button", { name: "View full message" })).toBeVisible();
  if (kind === "recovery") expect(view.getByText("Recovering · attempt 1/2")).toBeVisible();
  else if (kind === "async_question") expect(view.getByRole("region", { name: "Question for you" })).toBeVisible();
  else {
    // This same original final row would prove saved output without the flag.
    expect(presentOutcome({ ...message, outcome: { ...message.outcome!, recording_issues: {} } }, messages).outputSaved).toBe(true);
    fireEvent.click(view.getByText("Completion details"));
    expect(view.getByText("A declared final answer is missing from the saved transcript.")).toBeVisible();
    expect(view.queryByText("Model output was saved.")).not.toBeInTheDocument();
  }
});

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
