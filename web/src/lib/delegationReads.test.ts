import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSessionStore, type Delegation } from "../stores/sessionStore";
import { handleWsMessage } from "../hooks/useWebSocket";
import { observeDelegations } from "./delegationReads";

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
afterEach(() => vi.unstubAllGlobals());

it.each([false, true])("merges overlapping successful round reads when completed returns first=%s", async (completedFirst) => {
  const first = deferred(), second = deferred();
  vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise));
  const a = observeDelegations("parent"), b = observeDelegations("parent");
  if (completedFirst) {
    second.resolve(Response.json([round("original", "completed")])); await b.done;
    first.resolve(Response.json([round()])); await a.done;
  } else {
    first.resolve(Response.json([round()])); await a.done;
    second.resolve(Response.json([round("original", "completed")])); await b.done;
  }
  expect(useSessionStore.getState().delegations.parent[0].state).toBe("completed");
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
