import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

import { DispatchQueue } from "./DispatchQueue";
import { handleWsMessage } from "../hooks/useWebSocket";
import { useSessionStore } from "../stores/sessionStore";
import { selectSession } from "../lib/selectSession";
import { loadSessionSnapshot } from "../lib/hydrateSession";

const dispatch = {
  generation: "service-current", revision: 3, paused: false, active_request_id: null,
  items: [],
};
beforeEach(() => useSessionStore.setState({
  token: "bearer", dispatchStates: {}, dispatchGeneration: null, retiredDispatchGenerations: [],
  dispatchUnavailable: {}, messages: {}, lastAppliedSeq: {}, streamingText: {},
}));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("keeps a real recording conflict visible without offering a saving retry", () => {
  useSessionStore.getState().applyDispatchState("session", dispatch);
  handleWsMessage({ type: "recording_failed", session_id: "session", terminal: false,
    request_id: "finished-request", recording_state: "failed",
    message: "This request conflicts with an already recorded outcome." });
  render(<DispatchQueue sessionId="session" />);
  expect(useSessionStore.getState().messages.session.at(-1)?.content).toBe("This request conflicts with an already recorded outcome.");
  expect(screen.queryByRole("button")).toBeNull();
});

it("keeps the paused queue after an unsuccessful Resume", async () => {
  useSessionStore.getState().applyDispatchState("session", { ...dispatch, paused: true });
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "Storage still unavailable" }), { status: 503 })));
  render(<DispatchQueue sessionId="session" />);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Resume processing" })));
  expect(screen.getByText("Storage still unavailable")).toBeTruthy();
  expect(useSessionStore.getState().dispatchStates.session.paused).toBe(true);
  expect(screen.getByRole("button", { name: "Resume processing" })).not.toBeDisabled();
});

it("resumes processing only after the server confirms the current queue", async () => {
  const paused = { ...dispatch, paused: true, items: [
    { request_id: "pending", source: "bg", summary: "Waiting result", priority: false },
  ] };
  useSessionStore.getState().applyDispatchState("session", paused);
  let finish!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { finish = resolve; })));
  render(<DispatchQueue sessionId="session" />);
  fireEvent.click(screen.getByRole("button", { name: "Resume processing" }));
  expect(screen.getByRole("button", { name: "Resuming…" })).toBeDisabled();
  expect(useSessionStore.getState().dispatchStates.session.paused).toBe(true);
  await act(async () => finish(Response.json({ ...paused, revision: 4, paused: false })));
  expect(fetch).toHaveBeenCalledWith(expect.stringMatching(/\/api\/sessions\/session\/dispatch\/resume$/), expect.objectContaining({ method: "POST" }));
  expect(useSessionStore.getState().dispatchStates.session.paused).toBe(false);
  expect(screen.getByText("Waiting result")).toBeTruthy();
  expect(screen.queryByRole("button")).toBeNull();
});

it("waits for a real snapshot and then accepts an explicitly empty queue", () => {
  render(<DispatchQueue sessionId="session" />);
  expect(screen.getByRole("status")).toHaveTextContent("Loading queue state");
  act(() => useSessionStore.getState().applyDispatchState("session", { ...dispatch }));
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
  const snapshot = { ...dispatch, revision: 7 };
  handleWsMessage({ type: "dispatch_state", session_id: "session", dispatch_state: snapshot });
  let rejectEarlier!: (reason: Error) => void;
  vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => new Promise<Response>((_, reject) => { rejectEarlier = reject; }))
    .mockResolvedValueOnce(new Response("unavailable", { status: 503 }))
    .mockResolvedValueOnce(Response.json({ messages: [], dispatch_state: snapshot, active_attempt: null })));
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
  handleWsMessage({ type: "dispatch_state", session_id: "other", dispatch_state: dispatch });
  let finish!: (value: Response) => void;
  let reject!: (reason: Error) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((yes, no) => { finish = yes; reject = no; })));
  const earlier = loadSessionSnapshot("session").catch(() => {});
  render(<DispatchQueue sessionId="session" />);
  act(() => handleWsMessage({ type: "dispatch_state", session_id: "other",
    dispatch_state: { ...dispatch, generation: "new-service", revision: 0 } }));
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
  useSessionStore.getState().applyDispatchState("session", { ...dispatch, paused: true });
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ resumed: true })));
  render(<DispatchQueue sessionId="session" />);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Resume processing" })));
  expect(screen.getByRole("alert")).toHaveTextContent("Queue state unavailable");
  expect(screen.getByRole("button", { name: "Resume processing" })).not.toBeDisabled();
  expect(useSessionStore.getState().dispatchStates.session.paused).toBe(true);
});

it("ignores a malformed Resume response after a newer WS confirmation", async () => {
  useSessionStore.getState().applyDispatchState("session", { ...dispatch, paused: true });
  let finish!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { finish = resolve; })));
  render(<DispatchQueue sessionId="session" />);
  fireEvent.click(screen.getByRole("button", { name: "Resume processing" }));
  const confirmed = { ...dispatch, revision: 4, paused: false };
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
  const snapshot = { ...dispatch,
    items: [{ request_id: "queued", source: "bg", summary: "Retained work", priority: false }] };
  handleWsMessage({ type: "queued", session_id: "session", dispatch_state: snapshot });
  render(<DispatchQueue sessionId="session" />);
  act(() => handleWsMessage({ type: "dispatch_state", session_id: "session", dispatch_state: { generation: snapshot.generation } }));
  expect(screen.getByRole("alert")).toHaveTextContent("Queue state unavailable");
  expect(screen.getByText("Retained work")).toBeTruthy();
  act(() => handleWsMessage({ type: "dispatch_state", session_id: "session", dispatch_state: snapshot }));
  expect(screen.queryByRole("alert")).toBeNull();
});
