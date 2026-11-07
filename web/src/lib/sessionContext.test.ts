import { beforeEach, expect, it } from "vitest";
import { handleWsMessage } from "../hooks/useWebSocket";
import { useSessionStore } from "../stores/sessionStore";
import { applySessionSnapshot } from "./hydrateSession";
import { contextState } from "./sessionContext.fixture";

const dispatch = { generation: "service", revision: 2, paused: false, active_request_id: null, active_operation: null, items: [] };
beforeEach(() => {
  useSessionStore.setState(useSessionStore.getInitialState());
  useSessionStore.getState().applyDispatchState("session", dispatch);
});

it("keeps a newer complete context replacement when older HTTP detail arrives", () => {
  const start = useSessionStore.getState();
  const stale = { ...contextState(3), validity: "stale", invalidation: {
    reason: "configuration_changed", observed_at: "2026-11-07T07:01:00Z" }, sample: null };
  handleWsMessage({ type: "context_state", session_id: "session", generation: "service", context_state: stale });
  applySessionSnapshot("session", { dispatch_state: dispatch, active_attempt: null, context_generation: "service",
    context_state: contextState(2), messages: [] }, start);
  expect(useSessionStore.getState().contextStates.session.state).toEqual(stale);
  expect(useSessionStore.getState().dispatchStates.session.revision).toBe(2);
});

it("accepts explicit initial null and rejects invalid, retired and wrong-account observations", () => {
  const start = useSessionStore.getState();
  handleWsMessage({ type: "context_state", session_id: "session", generation: "service", context_state: null });
  expect(useSessionStore.getState().contextStates.session.state).toBeNull();
  handleWsMessage({ type: "context_state", session_id: "session", generation: "retired", context_state: contextState(99) });
  handleWsMessage({ type: "context_state", session_id: "session", generation: "service", context_state: {
    ...contextState(2), sample: { ...contextState().sample, used_tokens: -1 } } });
  expect(useSessionStore.getState().contextStates.session.state).toBeNull();
  useSessionStore.getState().setToken("other-account");
  applySessionSnapshot("session", { archived: true, messages: [], context_generation: "service", context_state: contextState() }, start);
  expect(useSessionStore.getState().contextStates.session).toBeUndefined();
});

it("keeps pending at the same revision and recovers through a newer trusted HTTP detail", () => {
  const detail = (revision: number, pending: boolean) => ({ dispatch_state: dispatch, active_attempt: null,
    context_generation: "service", context_state: contextState(revision), context_pending: pending, messages: [] });
  applySessionSnapshot("session", detail(3, false));
  const oldRead = useSessionStore.getState();
  applySessionSnapshot("session", detail(3, true));
  expect(useSessionStore.getState().contextStates.session.pending).toBe(true);
  applySessionSnapshot("session", detail(3, false), oldRead);
  expect(useSessionStore.getState().contextStates.session.pending).toBe(true);
  applySessionSnapshot("session", detail(4, false));
  expect(useSessionStore.getState().contextStates.session.pending).toBe(false);
  applySessionSnapshot("session", detail(3, true), oldRead);
  expect(useSessionStore.getState().contextStates.session.state?.revision).toBe(4);
  expect(useSessionStore.getState().contextStates.session.pending).toBe(false);
});

it("clears pending on a newer committed WS state and ignores malformed availability", () => {
  const detail = { dispatch_state: dispatch, active_attempt: null, context_generation: "service",
    context_state: contextState(2), context_pending: true, messages: [] };
  applySessionSnapshot("session", detail);
  handleWsMessage({ type: "context_state", session_id: "session", generation: "service", context_state: contextState(3) });
  expect(useSessionStore.getState().contextStates.session.pending).toBe(false);
  applySessionSnapshot("session", { ...detail, context_state: contextState(4), context_pending: "false" });
  expect(useSessionStore.getState().contextStates.session.state?.revision).toBe(3);
});
