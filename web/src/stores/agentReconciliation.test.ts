import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSessionStore, type Agent } from "./sessionStore";
import { refreshAgents, withAgentRequest } from "../lib/refreshAgents";
import { pinnedInOrder } from "../lib/sidebarPins";

const agent = (id: string, name = id, pin_order = 1): Agent => ({ id, name, pin_order, pinned: true, archived: false, backend: "codex" } as Agent);
beforeEach(() => useSessionStore.setState({ ...useSessionStore.getInitialState(), token: "synthetic-token", accountGeneration: 1, dispatchGeneration: "service-a", agents: [agent("a"), agent("b", "b", 2)] }));
afterEach(() => vi.unstubAllGlobals());

it.each(["older-first", "newer-first"])("keeps the latest HTTP intent with %s receipts", async (order) => {
  await withAgentRequest(async (older) => withAgentRequest(async (newer) => {
    const store = useSessionStore.getState();
    if (order === "older-first") store.upsertAgent(agent("a", "Older"), older);
    store.upsertAgent(agent("a", "Newer"), newer);
    if (order === "newer-first") store.upsertAgent(agent("a", "Older"), older);
    expect(useSessionStore.getState().agents.find((row) => row.id === "a")?.name).toBe("Newer");
  }));
});

it("a write receipt during a newer GET cannot be rolled back by that GET", async () => {
  await withAgentRequest(async (write) => withAgentRequest(async (read) => {
    const store = useSessionStore.getState();
    store.upsertAgent(agent("a", "Committed"), write);
    store.applyAgentSnapshot([agent("a", "Old"), agent("b"), agent("c", "Offline")], read);
    expect(useSessionStore.getState().agents.map((row) => row.name)).toEqual(["Committed", "b", "Offline"]);
  }, "read"));
});

it("a newer full snapshot proves absence even for an ID the browser never saw", async () => {
  await withAgentRequest(async (older) => withAgentRequest(async (newer) => {
    const store = useSessionStore.getState();
    store.applyAgentSnapshot([], newer);
    store.applyAgentSnapshot([agent("unseen", "Already archived offline")], older);
    expect(useSessionStore.getState().agents).toEqual([]);
  }, "read"), "read");
});

it("a transient create/delete remains protected until all older reads end", async () => {
  await withAgentRequest(async (read) => {
    const store = useSessionStore.getState();
    store.upsertAgent(agent("temporary"));
    store.removeAgent("temporary");
    store.applyAgentSnapshot([agent("temporary"), agent("c", "Offline")], read);
    expect(useSessionStore.getState().agents.map((row) => row.id)).toEqual(["c"]);
  }, "read");
  // No persistent tombstone: a later authoritative snapshot can add the ID.
  await withAgentRequest(async (read) => {
    useSessionStore.getState().applyAgentSnapshot([agent("temporary")], read);
    expect(useSessionStore.getState().agents[0].id).toBe("temporary");
  }, "read");
});

it("old sorting confirmation and compensation cannot undo the next optimistic sort", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json([agent("a", "a", 1), agent("b", "b", 2), agent("c", "Offline", 3)])));
  await withAgentRequest(async (older) => withAgentRequest(async (newer) => {
    const store = useSessionStore.getState();
    store.applyAgentOrder(["a", "b"], older);
    store.applyAgentOrder(["b", "a"], newer);
    store.applyAgentSnapshot([agent("a", "a", 1), agent("b", "b", 2), agent("c", "Offline", 3)], older);
    await refreshAgents(older);
    expect(pinnedInOrder(useSessionStore.getState().agents).map((row) => row.id)).toEqual(["b", "a", "c"]);
    store.applyAgentSnapshot([agent("b", "b", 1), agent("a", "a", 2), agent("c", "Offline", 3)], newer);
    expect(pinnedInOrder(useSessionStore.getState().agents).map((row) => row.id)).toEqual(["b", "a", "c"]);
  }));
});

it("a GET begun during optimistic ordering cannot suppress its later confirmation", async () => {
  await withAgentRequest(async (write) => {
    const store = useSessionStore.getState();
    store.applyAgentOrder(["b", "a"], write);
    await withAgentRequest(async (read) => {
      store.applyAgentSnapshot([agent("a", "a", 1), agent("b", "b", 2)], read);
    }, "read");
    store.applyAgentSnapshot([agent("a", "a", 2), agent("b", "b", 1)], write);
    expect(pinnedInOrder(useSessionStore.getState().agents).map((row) => row.id)).toEqual(["b", "a"]);
  });
});

it.each(["account", "service", "ended"])("discards %s request receipts", async (change) => {
  const store = useSessionStore.getState();
  const request = store.beginAgentRequest("read");
  try {
    if (change === "account") useSessionStore.setState({ accountGeneration: 2 });
    else if (change === "service") useSessionStore.setState({ dispatchGeneration: "service-b" });
    else store.endAgentRequest(request);
    store.applyAgentSnapshot([agent("old", "Obsolete")], request);
    expect(useSessionStore.getState().agents.map((row) => row.id)).toEqual(["a", "b"]);
  } finally { store.endAgentRequest(request); }
});
