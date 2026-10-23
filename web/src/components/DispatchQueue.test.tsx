import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

import { DispatchQueue } from "./DispatchQueue";
import { handleWsMessage } from "../hooks/useWebSocket";
import { useSessionStore } from "../stores/sessionStore";
import { selectSession } from "../lib/selectSession";
import { loadSessionSnapshot } from "../lib/hydrateSession";

const blocked = {
  generation: "service-current", revision: 3, paused: false, active_request_id: null,
  recording_failure: { request_id: "finished-request", state: "unconfirmed" }, items: [],
};
beforeEach(() => useSessionStore.setState({
  token: "bearer", dispatchStates: {}, dispatchGeneration: null, retiredDispatchGenerations: [],
  dispatchUnavailable: {}, messages: {}, lastAppliedSeq: {}, streamingText: {},
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

it("waits for a real snapshot and then accepts an explicitly empty queue", () => {
  render(<DispatchQueue sessionId="session" />);
  expect(screen.getByRole("status")).toHaveTextContent("Loading queue state");
  act(() => useSessionStore.getState().applyDispatchState("session", { ...blocked, recording_failure: null }));
  expect(screen.queryByRole("status")).toBeNull();
  expect(screen.queryByLabelText("Queued messages")).toBeNull();
});

it("shows an explicit failure when a live detail has no current dispatch snapshot", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => Response.json(url.endsWith("/bg-tasks") ? [] : {
    id: "session", archived: false, messages: [], pending_queue: ["Obsolete queue text"],
  })));
  render(<DispatchQueue sessionId="session" />);
  await act(async () => selectSession("session"));
  expect(screen.getByRole("alert")).toHaveTextContent(/queue state unavailable/i);
  expect(screen.queryByText("Obsolete queue text")).toBeNull();
});

it.each(["HTTP", "WS"])("recovers on an equal revision from %s without letting an earlier read fail it again", async (source) => {
  const snapshot = { ...blocked, revision: 7, recording_failure: null };
  handleWsMessage({ type: "dispatch_state", session_id: "session", dispatch_state: snapshot });
  let rejectEarlier!: (reason: Error) => void;
  vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => new Promise<Response>((_, reject) => { rejectEarlier = reject; }))
    .mockResolvedValueOnce(new Response("unavailable", { status: 503 }))
    .mockResolvedValueOnce(Response.json({ messages: [], dispatch_state: snapshot })));
  render(<DispatchQueue sessionId="session" />);
  const earlier = loadSessionSnapshot("session").catch(() => {});
  await act(async () => { await loadSessionSnapshot("session").catch(() => {}); });
  expect(screen.getByRole("alert")).toHaveTextContent("Queue state unavailable");
  act(() => handleWsMessage({ type: "dispatch_state", session_id: "session", dispatch_state: { ...snapshot, revision: 6 } }));
  expect(screen.getByRole("alert")).toHaveTextContent("Queue state unavailable");
  await act(async () => {
    if (source === "HTTP") await loadSessionSnapshot("session");
    else handleWsMessage({ type: "dispatch_state", session_id: "session", dispatch_state: snapshot });
  });
  expect(screen.queryByRole("alert")).toBeNull();
  await act(async () => { rejectEarlier(new Error("late failure")); await earlier; });
  expect(screen.queryByRole("alert")).toBeNull();
});

it.each(["network", "malformed"])("ignores an old %s failure after another session announces a new service", async (kind) => {
  handleWsMessage({ type: "dispatch_state", session_id: "other", dispatch_state: blocked });
  let finish!: (value: Response) => void;
  let reject!: (reason: Error) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((yes, no) => { finish = yes; reject = no; })));
  const earlier = loadSessionSnapshot("session").catch(() => {});
  render(<DispatchQueue sessionId="session" />);
  act(() => handleWsMessage({ type: "dispatch_state", session_id: "other",
    dispatch_state: { ...blocked, generation: "new-service", revision: 0 } }));
  await act(async () => {
    if (kind === "network") reject(new Error("late failure"));
    else finish(Response.json({ messages: [], dispatch_state: { generation: "service-current" } }));
    await earlier;
  });
  expect(screen.queryByRole("alert")).toBeNull();
  act(() => handleWsMessage({ type: "dispatch_state", session_id: "session", dispatch_state: { generation: "service-current" } }));
  expect(screen.queryByRole("alert")).toBeNull();
});

it("reports a malformed successful Resume response and retains the paused queue", async () => {
  useSessionStore.getState().applyDispatchState("session", { ...blocked, paused: true, recording_failure: null });
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ resumed: true })));
  render(<DispatchQueue sessionId="session" />);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Resume processing" })));
  expect(screen.getByRole("alert")).toHaveTextContent("Queue state unavailable");
  expect(screen.getByRole("button", { name: "Resume processing" })).not.toBeDisabled();
  expect(useSessionStore.getState().dispatchStates.session.paused).toBe(true);
});

it("ignores a malformed Resume response after a newer WS confirmation", async () => {
  useSessionStore.getState().applyDispatchState("session", { ...blocked, paused: true, recording_failure: null });
  let finish!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { finish = resolve; })));
  render(<DispatchQueue sessionId="session" />);
  fireEvent.click(screen.getByRole("button", { name: "Resume processing" }));
  const confirmed = { ...blocked, revision: 4, paused: false, recording_failure: null };
  act(() => handleWsMessage({ type: "dispatch_state", session_id: "session", dispatch_state: confirmed }));
  await act(async () => { finish(Response.json({ resumed: true })); });
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.queryByLabelText("Queued messages")).toBeNull();
  expect(useSessionStore.getState().dispatchStates.session).toEqual(confirmed);
});

it("finishes archived viewing without an active snapshot or loading state", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => Response.json(url.endsWith("/bg-tasks") ? [] : {
    id: "session", archived: true, messages: [], dispatch_state: null,
  })));
  render(<DispatchQueue sessionId="session" archived readOnly />);
  await act(async () => { await loadSessionSnapshot("session"); });
  expect(screen.queryByRole("status")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(useSessionStore.getState().dispatchStates.session).toBeUndefined();
});

it("keeps the last queue visible through an invalid live frame and recovers on confirmation", () => {
  const snapshot = { ...blocked, recording_failure: null,
    items: [{ request_id: "queued", source: "bg", summary: "Retained work", priority: false }] };
  handleWsMessage({ type: "queued", session_id: "session", dispatch_state: snapshot });
  render(<DispatchQueue sessionId="session" />);
  act(() => handleWsMessage({ type: "dispatch_state", session_id: "session", dispatch_state: { generation: snapshot.generation } }));
  expect(screen.getByRole("alert")).toHaveTextContent("Queue state unavailable");
  expect(screen.getByText("Retained work")).toBeTruthy();
  act(() => handleWsMessage({ type: "dispatch_state", session_id: "session", dispatch_state: snapshot }));
  expect(screen.queryByRole("alert")).toBeNull();
});
