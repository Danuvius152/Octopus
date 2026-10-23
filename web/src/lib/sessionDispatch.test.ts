import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { handleWsMessage } from "../hooks/useWebSocket";
import { useSessionStore } from "../stores/sessionStore";
import { selectSession } from "./selectSession";

const snapshot = (generation = "service-a", revision = 2, paused = true) => ({
  generation, revision, paused, active_request_id: null,
  items: [{ request_id: "bg-1", source: "bg", summary: "completed command", priority: false }],
});

beforeEach(() => useSessionStore.setState({
  token: "test-token", sessions: [], messages: {}, dispatchStates: {}, dispatchGeneration: null,
  retiredDispatchGenerations: [], dispatchUnavailable: {}, lastAppliedSeq: {},
  pendingQuestions: {},
}));
afterEach(() => vi.unstubAllGlobals());

it("loads dispatch pause on selection and keeps a newer live snapshot", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => new Response(JSON.stringify(
    url.endsWith("/bg-tasks") ? [] : {
      id: "selected", messages: [], dispatch_state: snapshot(),
    }
  ))));
  await selectSession("selected");
  expect(useSessionStore.getState().dispatchStates.selected).toEqual(snapshot());
  handleWsMessage({ type: "dispatch_state", session_id: "selected", dispatch_state: snapshot("service-a", 4) });
  await selectSession("selected");
  expect(useSessionStore.getState().dispatchStates.selected.revision).toBe(4);
  expect(useSessionStore.getState().dispatchStates.selected.items[0].summary).toBe("completed command");
});

it("accepts a restarted service and rejects a delayed retired service across sessions", () => {
  handleWsMessage({ type: "dispatch_state", session_id: "selected", dispatch_state: snapshot() });
  handleWsMessage({ type: "dispatch_state", session_id: "selected", dispatch_state: { ...snapshot("service-b", 0, false), items: [] } });
  handleWsMessage({ type: "dispatch_state", session_id: "other", dispatch_state: snapshot("service-a", 100) });
  expect(useSessionStore.getState().dispatchStates.selected.paused).toBe(false);
  expect(useSessionStore.getState().dispatchStates.other).toBeUndefined();
});

it("ignores an older-instance HTTP response that finishes after a fresh websocket", async () => {
  let finish!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn((url: string) => url.endsWith("/bg-tasks")
    ? Promise.resolve(new Response("[]")) : new Promise<Response>((resolve) => { finish = resolve; })));
  const loading = selectSession("selected");
  handleWsMessage({ type: "dispatch_state", session_id: "selected", dispatch_state: { ...snapshot("service-b", 0, false), items: [] } });
  finish(new Response(JSON.stringify({ id: "selected", messages: [], dispatch_state: snapshot("service-a", 100),
    pending_questions: [{ question_id: "retired-question", questions: [{ question: "Old question" }] }],
  })));
  await loading;
  expect(useSessionStore.getState().dispatchGeneration).toBe("service-b");
  expect(useSessionStore.getState().dispatchStates.selected.paused).toBe(false);
  expect(useSessionStore.getState().pendingQuestions.selected ?? []).toEqual([]);
});
