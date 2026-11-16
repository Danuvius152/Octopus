import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSessionStore, type Delegation, type SessionInfo } from "../stores/sessionStore";
import { sessions } from "./sessionList";
import { handleWsMessage } from "../hooks/useWebSocket";
import { observeDelegations, refreshDelegations, watchDelegation } from "./delegationReads";

function deferred() {
  let resolve!: (value: Response) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<Response>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const round = (work_id = "original", state: Delegation["state"] = "running"): Delegation => ({
  delegation_id: "child", work_id, state, parent_session_id: "parent",
    sub_session_id: "child", target_agent_id: "agent", target_agent_name: "Helper", request: "Work",
    created_at: "now", finished_at: null, error: null,
});
beforeEach(() => {
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useSessionStore.setState({ token: "test", dispatchGeneration: "service" });
  useSessionStore.getState().setDelegations("parent", [round()]);
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

it.each(["running", "completed", "failed", "cancelled", "interrupted"] as const)("accepts the %s HTTP delegation state", async (state) => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json([round("original", state)])));
  const read = observeDelegations("parent");
  expect(await read.done).toEqual([round("original", state)]);
});

it.each(["invented", "toString", null, 3])("rejects an invalid HTTP delegation state %s", async (state) => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json([{ ...round(), state }])));
  await expect(observeDelegations("parent").done).rejects.toThrow("Could not read delegations.");
  expect(useSessionStore.getState().delegations.parent[0].state).toBe("running");
});

it("replaces an aborted ordinary flight while a forced read keeps the coordinator alive without spending failure budget", async () => {
  vi.useFakeTimers();
  const held = new Map<number, ReturnType<typeof deferred>>();
  let calls = 0;
  vi.stubGlobal("fetch", vi.fn((_url, init) => {
    const call = ++calls;
    if (call === 1 || call === 4) return Promise.resolve(Response.json({}, { status: 503 }));
    const response = deferred(); held.set(call, response);
    init.signal.addEventListener("abort", () => response.reject(new DOMException("No readers", "AbortError")), { once: true });
    return response.promise;
  }));
  const old = watchDelegation("parent", round(), () => {});
  await vi.advanceTimersByTimeAsync(2000);
  expect(calls).toBe(2);
  const forced = refreshDelegations("parent");
  old.cancel();
  const status = vi.fn();
  const next = watchDelegation("parent", round(), status);
  try {
    expect(calls).toBe(4);
    await vi.advanceTimersByTimeAsync(2000);
    expect(calls).toBe(5); // One network failure + one network failure; abort is not a third failure.
    expect(status).not.toHaveBeenCalledWith(true);
    held.get(5)!.resolve(Response.json([round("original", "completed")]));
    await vi.advanceTimersByTimeAsync(0);
    expect(useSessionStore.getState().delegations.parent[0].state).toBe("completed");
  } finally { old.cancel(); forced.cancel(); next.cancel(); }
});

it("starts a new target observation instead of joining a retired target's old request", async () => {
  const old = deferred(), fresh = deferred();
  vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise));
  useSessionStore.setState({ sessions: [{ id: "parent", name: "Parent" } as SessionInfo] });
  const beforeArchive = observeDelegations("parent");
  sessions.archive("parent");
  const afterArchive = observeDelegations("parent");
  try {
    expect(fetch).toHaveBeenCalledTimes(2);
    fresh.resolve(Response.json([round("new-observation", "completed")]));
    await afterArchive.done;
    old.resolve(Response.json([round("retired-observation", "completed")]));
    expect(await beforeArchive.done).toBeNull();
    expect(useSessionStore.getState().delegations.parent.map((row) => row.work_id)).toEqual(["new-observation"]);
  } finally { beforeArchive.cancel(); afterArchive.cancel(); }
});

it("retires an exhausted observation before its aborted confirmation settles", async () => {
  vi.useFakeTimers();
  const held = [deferred(), deferred()];
  let calls = 0;
  vi.stubGlobal("fetch", vi.fn(() => {
    calls++;
    return calls <= 3 ? Promise.resolve(Response.json({}, { status: 503 })) : held[calls - 4].promise;
  }));
  const status = vi.fn();
  const previous = watchDelegation("parent", round(), status);
  await vi.advanceTimersByTimeAsync(4000);
  expect(calls).toBe(3);
  expect(status).toHaveBeenLastCalledWith(true);
  const confirmation = refreshDelegations("parent");
  previous.cancel(); confirmation.cancel();
  const next = watchDelegation("parent", round(), () => {});
  try {
    expect(calls).toBe(5);
    held[0].resolve(Response.json({}, { status: 503 }));
    await confirmation.done;
    const join = observeDelegations("parent");
    expect(calls).toBe(5);
    held[1].resolve(Response.json([round()]));
    await join.done;
    expect(useSessionStore.getState().delegations.parent[0].state).toBe("running");
  } finally { next.cancel(); }
});

it("does not lend a retired target's exhausted budget to a new target observation", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({}, { status: 503 })));
  useSessionStore.setState({ sessions: [{ id: "parent", name: "Parent" } as SessionInfo] });
  const previous = watchDelegation("parent", round(), () => {});
  await vi.advanceTimersByTimeAsync(4000);
  expect(fetch).toHaveBeenCalledTimes(3);
  sessions.archive("parent");
  const next = watchDelegation("parent", round(), () => {});
  try { expect(fetch).toHaveBeenCalledTimes(4); }
  finally { previous.cancel(); next.cancel(); }
});

it.each([false, true])("merges overlapping successful round reads when completed returns first=%s", async (completedFirst) => {
  const first = deferred(), second = deferred();
  vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise));
  const a = observeDelegations("parent"), b = refreshDelegations("parent");
  if (completedFirst) {
    second.resolve(Response.json([round("original", "completed")])); await b.done;
    first.resolve(Response.json([round()])); await a.done;
  } else {
    first.resolve(Response.json([round()])); await a.done;
    second.resolve(Response.json([round("original", "completed")])); await b.done;
  }
  expect(useSessionStore.getState().delegations.parent[0].state).toBe("completed");
});

it("shares an old GET with a later consumer without borrowing its new baseline or confirmation order", async () => {
  const old = deferred(), fresh = deferred();
  vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise));
  const hydration = observeDelegations("parent");
  handleWsMessage({ type: "delegation_delivery", session_id: "parent", delegation: {
    ...round(), terminal_disposition: "unknown", terminal_delivery_error: "New WS fact",
  } });
  const late = watchDelegation("parent", round(), () => {});
  expect(fetch).toHaveBeenCalledTimes(1);
  const confirmation = refreshDelegations("parent");
  expect(fetch).toHaveBeenCalledTimes(2);
  fresh.resolve(Response.json([{ ...round(), terminal_delivery_error: "Fresh HTTP fact" }]));
  await confirmation.done;
  old.resolve(Response.json([{ ...round(), terminal_delivery_error: null }]));
  await hydration.done;
  expect(useSessionStore.getState().delegations.parent[0].terminal_delivery_error).toBe("Fresh HTTP fact");
  late.cancel();
});

it("keeps the original fetch baseline when a card joins after a live event", async () => {
  const response = deferred();
  vi.stubGlobal("fetch", vi.fn(() => response.promise));
  const first = observeDelegations("parent");
  handleWsMessage({ type: "delegation_delivery", session_id: "parent", delegation: {
    ...round(), terminal_delivery_error: "New live fact",
  } });
  const late = watchDelegation("parent", round(), () => {});
  response.resolve(Response.json([{ ...round(), terminal_delivery_error: null }]));
  await first.done;
  expect(useSessionStore.getState().delegations.parent[0].terminal_delivery_error).toBe("New live fact");
  late.cancel();
});

it("cancels a shared fetch only after its final consumer leaves and allows a new observation", async () => {
  const replies = [deferred(), deferred()];
  const signals: AbortSignal[] = [];
  vi.stubGlobal("fetch", vi.fn((_url, init) => {
    signals.push(init.signal);
    return replies[signals.length - 1].promise;
  }));
  const first = observeDelegations("parent"), second = observeDelegations("parent");
  first.cancel();
  expect(signals[0].aborted).toBe(false);
  second.cancel();
  expect(signals[0].aborted).toBe(true);
  const next = observeDelegations("parent");
  expect(fetch).toHaveBeenCalledTimes(2);
  replies[0].resolve(Response.json([round("retired")]));
  replies[1].resolve(Response.json([round("fresh", "completed")]));
  await Promise.all([first.done, second.done, next.done]);
  expect(useSessionStore.getState().delegations.parent.map((row) => row.work_id)).toEqual(["fresh"]);
});

it("keeps an exact round's live diagnostics and a new running work across its old list", async () => {
  const response = deferred();
  vi.stubGlobal("fetch", vi.fn(() => response.promise));
  const read = observeDelegations("parent");
  handleWsMessage({ type: "delegation_delivery", session_id: "parent", delegation: {
    ...round("original", "completed"), terminal_disposition: "unknown", terminal_delivery_error: "ACK lost",
  } });
  handleWsMessage({ type: "delegation_delivery", session_id: "parent", delegation: round("next") });
  response.resolve(Response.json([round()]));
  await read.done;
  expect(useSessionStore.getState().delegations.parent).toEqual(expect.arrayContaining([
    expect.objectContaining({ work_id: "original", state: "completed", terminal_delivery_error: "ACK lost" }),
    expect.objectContaining({ work_id: "next", state: "running" }),
  ]));
});

it.each(["cancel", "service", "account"])("does not apply a late response after %s", async (change) => {
  const response = deferred();
  vi.stubGlobal("fetch", vi.fn(() => response.promise));
  const read = observeDelegations("parent");
  if (change === "cancel") read.cancel();
  else if (change === "account") useSessionStore.getState().setToken("other");
  else useSessionStore.getState().applyDispatchState("other", { generation: "new", revision: 0, paused: false, active_request_id: null, active_operation: null, items: [] });
  response.resolve(Response.json([round("retired", "completed")]));
  await read.done;
  expect(useSessionStore.getState().delegations.parent?.some((item) => item.work_id === "retired")).toBeFalsy();
});
