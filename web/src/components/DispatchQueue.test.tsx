import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

import { DispatchQueue } from "./DispatchQueue";
import { handleWsMessage } from "../hooks/useWebSocket";
import { useSessionStore } from "../stores/sessionStore";

const blocked = {
  generation: "service-current", revision: 3, paused: false, active_request_id: null,
  recording_failure: { request_id: "finished-request", state: "unconfirmed" }, items: [],
};
beforeEach(() => useSessionStore.setState({
  token: "bearer", dispatchStates: {}, dispatchGeneration: null, retiredDispatchGenerations: [],
  pendingQueue: {}, messages: {}, lastAppliedSeq: {}, streamingText: {},
}));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("shows saving uncertainty from a nonterminal notification, including an empty queue", () => {
  handleWsMessage({ type: "recording_failed", session_id: "session", terminal: false,
    message: "Result saving failed or is unconfirmed", dispatch_state: blocked,
    outcome: { status: "success", request_id: "finished-request" } });
  render(<DispatchQueue sessionId="session" />);
  expect(screen.getByRole("alert")).toHaveTextContent("Result saving failed or is unconfirmed");
  expect(screen.getByRole("button", { name: "Retry saving" })).toBeTruthy();
  expect(useSessionStore.getState().messages.session ?? []).toHaveLength(0);
});

it("checks saved state using Resume while preserving a separately paused queue", async () => {
  const paused = { ...blocked, paused: true, items: [
    { request_id: "pending", source: "bg", summary: "Waiting result", priority: false },
  ] };
  useSessionStore.getState().applyDispatchState("session", paused);
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
    ...paused, revision: 4, recording_failure: null,
  }))));
  render(<DispatchQueue sessionId="session" />);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry saving" })));
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/api/sessions/session/dispatch/resume?resume_queue=false"), expect.objectContaining({ method: "POST" }));
  expect(useSessionStore.getState().dispatchStates.session.paused).toBe(true);
  expect(screen.getByText("Waiting result")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Resume processing" })).toBeTruthy();
  expect(screen.queryByText("Result saving failed or is unconfirmed.")).toBeNull();
});

it("keeps the saving block after an unsuccessful check", async () => {
  useSessionStore.getState().applyDispatchState("session", blocked);
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "Storage still unavailable" }), { status: 503 })));
  render(<DispatchQueue sessionId="session" />);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry saving" })));
  expect(screen.getByText("Storage still unavailable")).toBeTruthy();
  expect(useSessionStore.getState().dispatchStates.session.recording_failure).toEqual(blocked.recording_failure);
  expect(screen.getByRole("button", { name: "Retry saving" })).not.toBeDisabled();
});

it("uses legacy queue only until a modern snapshot arrives, including empty items", () => {
  useSessionStore.getState().setPendingQueue("session", ["Legacy waiting"]);
  render(<DispatchQueue sessionId="session" />);
  expect(screen.getByText("Legacy waiting")).toBeTruthy();
  act(() => useSessionStore.getState().applyDispatchState("session", { ...blocked, recording_failure: null }));
  expect(screen.queryByText("Legacy waiting")).toBeNull();
  expect(useSessionStore.getState().pendingQueue.session).toBeUndefined();
  act(() => useSessionStore.getState().setPendingQueue("session", ["Late legacy response"]));
  expect(screen.queryByText("Late legacy response")).toBeNull();
  act(() => {
    useSessionStore.getState().setPendingQueue("other", ["Old service queue"]);
    useSessionStore.getState().applyDispatchState("session", { ...blocked, generation: "new-service", revision: 0, recording_failure: null });
  });
  expect(useSessionStore.getState().pendingQueue).toEqual({});
});
