import { beforeEach, expect, it } from "vitest";
import { applyTranscript } from "./transcript";
import { useSessionStore } from "../stores/sessionStore";
import type { MessageInput } from "./turnOutcome";

const rows = (first: number, count: number) => Array.from({ length: count }, (_, index) => ({
  role: "assistant" as const, type: "text", seq: first + index, content: `Saved text ${first + index}`,
  runtime_metadata: { request_id: "request", attempt_id: "attempt", provider: "codex", thread_id: "thread" },
}));

beforeEach(() => { useSessionStore.setState(useSessionStore.getInitialState(), true); });

it.each([0, 800])("reuses all equivalent rows and the array with %s loaded prefix rows", (prefix) => {
  const messages = rows(0, prefix + 200);
  expect(applyTranscript("session", { messages, next_message_seq: prefix + 200, oldest_loaded_seq: 0, has_more_messages: false })).toBe(true);
  const before = useSessionStore.getState().messages.session;
  const previousWindow = useSessionStore.getState().messageWindows.session;
  const fresh = JSON.parse(JSON.stringify(messages.slice(prefix)));
  expect(applyTranscript("session", { messages: fresh, next_message_seq: prefix + 200,
    oldest_loaded_seq: prefix, has_more_messages: prefix > 0 })).toBe(true);
  const after = useSessionStore.getState().messages.session;
  expect(after).toHaveLength(prefix + 200);
  expect(after.every((row, index) => row === before[index])).toBe(true);
  expect(after).toBe(before);
  expect(useSessionStore.getState().messageWindows.session).toBe(previousWindow);
  expect(useSessionStore.getState().oldestSeq.session).toBe(0);
  expect(useSessionStore.getState().hasMoreMessages.session).toBe(false);
});

const detailed: MessageInput = {
  ...rows(0, 1)[0], content: "Tool preview", type: "tool_result", body_state: "preview", detail_ref: "original-detail",
  tool_name: "read", tool_use_id: "tool-original", tool_input: { options: { lines: [1, 2], mode: "plain" } },
  outcome: { status: "failed", request_id: "request", code: "attachment_unavailable", recovery: { upload: 0 } },
  recovery: { phase: "diagnostic", message: "Original diagnostic", attempt: 1 },
  attachments: [{ id: "file", filename: "original.txt", size: 5, mime_type: "text/plain" }],
  created_at: "2026-11-17T00:00:00Z",
};

it("shares normalized nested message values independently of JSON property order", () => {
  applyTranscript("session", { messages: [detailed, ...rows(1, 1)], next_message_seq: 2, oldest_loaded_seq: 0 });
  const before = useSessionStore.getState().messages.session;
  const copied = JSON.parse(JSON.stringify(detailed)) as MessageInput;
  copied.tool_input = { options: { mode: "plain", lines: [1, 2] } };
  applyTranscript("session", { messages: [copied, ...rows(1, 1)], next_message_seq: 2, oldest_loaded_seq: 0 });
  expect(useSessionStore.getState().messages.session).toBe(before);
});

it.each([
  ["same-height body", { content: "New preview!" }],
  ["tool input", { tool_input: { options: { lines: [2, 1], mode: "plain" } } }],
  ["tool identity", { tool_use_id: "different-tool" }],
  ["outcome", { outcome: { ...detailed.outcome!, recovery: { upload: 1 } } }],
  ["recovery", { recovery: { ...detailed.recovery!, attempt: 2 } }],
  ["runtime metadata", { runtime_metadata: { ...detailed.runtime_metadata!, attempt_id: "different-attempt" } }],
  ["attachment", { attachments: [{ ...detailed.attachments![0], filename: "changed.txt" }] }],
  ["preview", { body_state: "inline" }],
  ["detail reference", { detail_ref: "different-detail" }],
  ["error", { is_error: true }],
  ["time", { created_at: "2026-11-17T00:01:00Z" }],
] satisfies [string, Partial<MessageInput>][])("publishes the changed %s and retains the other saved row", (_, update) => {
  applyTranscript("session", { messages: [detailed, ...rows(1, 1)], next_message_seq: 2, oldest_loaded_seq: 0 });
  const before = useSessionStore.getState().messages.session;
  applyTranscript("session", { messages: [{ ...detailed, ...update }, ...rows(1, 1)], next_message_seq: 2, oldest_loaded_seq: 0 });
  const after = useSessionStore.getState().messages.session;
  expect(after).not.toBe(before);
  expect(after[0]).not.toBe(before[0]);
  expect(after[1]).toBe(before[1]);
});

it("keeps duplicate unsequenced occurrences distinct across moves, removal and same-text reappearance", () => {
  const notice: MessageInput = { role: "system", type: "notice", content: "Repeated notice" };
  applyTranscript("session", { messages: [notice, { ...notice }, ...rows(1, 1)], next_message_seq: 2, oldest_loaded_seq: 1 });
  const before = useSessionStore.getState().messages.session;
  expect(before[1]).not.toBe(before[2]);
  applyTranscript("session", { messages: [...rows(0, 2), { ...notice }, { ...notice }], next_message_seq: 2, oldest_loaded_seq: 0 });
  const moved = useSessionStore.getState().messages.session;
  expect(moved[1]).toBe(before[0]);
  expect(moved[2]).not.toBe(before[1]);
  expect(moved[3]).not.toBe(before[2]);
  applyTranscript("session", { messages: rows(0, 2), next_message_seq: 2, oldest_loaded_seq: 0 });
  applyTranscript("session", { messages: [...rows(0, 2), { ...notice }], next_message_seq: 2, oldest_loaded_seq: 0 });
  const reappeared = useSessionStore.getState().messages.session;
  expect(reappeared[2]).not.toBe(moved[2]);
  expect(reappeared[2]).not.toBe(moved[3]);
});

it("settles an overlapping terminal page and its cursor while retaining the complete row array", () => {
  const identity = { request_id: "original-request", attempt_id: "original-attempt" };
  const terminal: MessageInput = { role: "assistant", type: "result", seq: 8, runtime_metadata: identity,
    content: { kind: "outcome", status: "success", request_id: identity.request_id } };
  applyTranscript("session", { messages: [terminal, ...rows(9, 2)], oldest_loaded_seq: 8, next_message_seq: 11 });
  const before = useSessionStore.getState();
  // The rows can precede an older page's cursor settlement. Retained live
  // previews still need their original terminal cleanup even with no new row.
  useSessionStore.setState({ oldestSeq: { session: 10 }, hasMoreMessages: { session: true },
    streamingText: { session: { content: "Retained root preview", attempt: identity } },
    streamingItems: { session: { old: { content: "Retained native preview", runtime_metadata: identity },
      current: { content: "Another attempt", runtime_metadata: { request_id: "current-request", attempt_id: "current-attempt" } } } } });
  expect(useSessionStore.getState().prependMessages("session", [terminal, ...rows(9, 1)], 8, false,
    { window: before.messageWindows.session, beforeSeq: 10 })).toEqual({ state: "merged", added: 0 });
  const after = useSessionStore.getState();
  expect(after.messages.session).toBe(before.messages.session);
  expect(after.oldestSeq.session).toBe(8);
  expect(after.hasMoreMessages.session).toBe(false);
  expect(after.lastAppliedSeq.session).toBe(10);
  expect(after.streamingText.session).toBeUndefined();
  expect(Object.values(after.streamingItems.session).map(item => item.content)).toEqual(["Another attempt"]);
});
