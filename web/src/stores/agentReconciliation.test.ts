import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSessionStore, type Agent } from "./sessionStore";
import { refreshAgents, withAgentRequest } from "../lib/refreshAgents";
import { pinnedInOrder } from "../lib/sidebarPins";
import { deferred } from "../test-support/deferred";

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
  }));
});

it("a newer full snapshot proves absence even for an ID the browser never saw", async () => {
  await withAgentRequest(async (older) => withAgentRequest(async (newer) => {
    const store = useSessionStore.getState();
    store.applyAgentSnapshot([], newer);
    store.applyAgentSnapshot([agent("unseen", "Already archived offline")], older);
    expect(useSessionStore.getState().agents).toEqual([]);
  }));
});

it("a transient create/delete remains protected until all older reads end", async () => {
  await withAgentRequest(async (read) => {
    const store = useSessionStore.getState();
    store.upsertAgent(agent("temporary"));
    store.removeAgent("temporary");
    store.applyAgentSnapshot([agent("temporary"), agent("c", "Offline")], read);
    expect(useSessionStore.getState().agents.map((row) => row.id)).toEqual(["c"]);
  });
  // No persistent tombstone: a later authoritative snapshot can add the ID.
  await withAgentRequest(async (read) => {
    useSessionStore.getState().applyAgentSnapshot([agent("temporary")], read);
    expect(useSessionStore.getState().agents[0].id).toBe("temporary");
  });
});

it("old sorting confirmation and compensation cannot undo the next optimistic sort", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json([agent("a", "a", 1), agent("b", "b", 2), agent("c", "Offline", 3)])));
  await withAgentRequest(async (older) => withAgentRequest(async (newer) => {
    const store = useSessionStore.getState();
    store.applyAgentOrder(["a", "b"], older);
    store.applyAgentOrder(["b", "a"], newer);
    store.confirmAgentOrder(["a", "b"], older);
    store.applyAgentSnapshot([agent("a", "a", 1), agent("b", "b", 2), agent("c", "Offline", 3)], older);
    await refreshAgents(older);
    expect(pinnedInOrder(useSessionStore.getState().agents).map((row) => row.id)).toEqual(["b", "a", "c"]);
    store.confirmAgentOrder(["b", "a"], newer);
    store.applyAgentSnapshot([agent("b", "b", 1), agent("a", "a", 2), agent("c", "Offline", 3)], newer);
    expect(pinnedInOrder(useSessionStore.getState().agents).map((row) => row.id)).toEqual(["b", "a", "c"]);
  }));
});

it("a failed later sort can recover after an older success is blocked and its confirmation is delayed", async () => {
  const confirmation = deferred<Response>();
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn(async () => ++reads === 1 ? confirmation.promise : Response.json([agent("a"), agent("b", "b", 2)])));
  const store = useSessionStore.getState();
  useSessionStore.setState({ archivedAgentsRead: { loaded: true, loading: false, invalidated: false, error: null } });
  await withAgentRequest(async (older) => {
    store.applyAgentOrder(["a", "b"], older);
    let confirming!: Promise<void>;
    await withAgentRequest(async (newer) => {
      store.applyAgentOrder(["b", "a"], newer);
      store.confirmAgentOrder(["a", "b"], older);
      store.applyAgentSnapshot([agent("a"), agent("b", "b", 2)], older, "live");
      confirming = refreshAgents(older, "all");
      await refreshAgents(newer, "all");
      expect(pinnedInOrder(useSessionStore.getState().agents).map((row) => row.id)).toEqual(["a", "b"]);
    }, "all");
    confirmation.resolve(Response.json([agent("a"), agent("b", "b", 2)]));
    await confirming;
    expect(pinnedInOrder(useSessionStore.getState().agents).map((row) => row.id)).toEqual(["a", "b"]);
    expect(useSessionStore.getState().archivedAgentsRead).toEqual({ loaded: true, loading: false, invalidated: false, error: null });
  }, "all");
});

it("an ended sort protects only submitted positions in a still pending GET", async () => {
  const store = useSessionStore.getState();
  const sorting = store.beginAgentRequest();
  const read = store.beginAgentRequest();
  try {
    store.applyAgentOrder(["b", "a"], sorting);
    store.confirmAgentOrder(["b", "a"], sorting);
    store.applyAgentSnapshot([agent("a", "Old", 1), agent("b", "Old", 2), agent("c", "Observed", 3)], sorting);
    store.endAgentRequest(sorting);
    store.applyAgentSnapshot([agent("a", "New name", 1), agent("b", "b", 2), agent("c", "New elsewhere", 4)], read);
    expect(pinnedInOrder(useSessionStore.getState().agents).map((row) => [row.id, row.pin_order])).toEqual([["b", 1], ["a", 2], ["c", 4]]);
    expect(useSessionStore.getState().agents.find((row) => row.id === "a")?.name).toBe("New name");
    expect(useSessionStore.getState().agents.find((row) => row.id === "c")?.name).toBe("New elsewhere");
  } finally { store.endAgentRequest(sorting); store.endAgentRequest(read); }
});

it("a completed newer full observation does not revoke a pending business acknowledgment", async () => {
  const store = useSessionStore.getState();
  await withAgentRequest(async (write) => {
    await withAgentRequest(async (read) => store.applyAgentSnapshot([{ ...agent("a"), archived: true }, agent("b")], read, "all"), "all");
    store.upsertAgent(agent("a", "Restored by original operation"), write);
    expect(useSessionStore.getState().agents.find((row) => row.id === "a")?.name).toBe("Restored by original operation");
    expect(useSessionStore.getState().archivedAgents).toEqual([]);
  });
});

it("a later compensation phase retains newer business and hard-delete protections", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json([agent("a", "Stale"), agent("b"), agent("offline")])));
  const store = useSessionStore.getState();
  await withAgentRequest(async (write) => {
    await withAgentRequest(async (newer) => store.upsertAgent(agent("a", "Committed"), newer));
    store.removeAgent("b");
    await refreshAgents(write, "all");
    expect(useSessionStore.getState().agents.map((row) => [row.id, row.name])).toEqual([["a", "Committed"], ["offline", "offline"]]);
  });
});

it("a GET begun during optimistic ordering cannot suppress its later confirmation", async () => {
  await withAgentRequest(async (write) => {
    const store = useSessionStore.getState();
    store.applyAgentOrder(["b", "a"], write);
    await withAgentRequest(async (read) => {
      store.applyAgentSnapshot([agent("a", "Read name", 1), agent("b", "b", 2)], read);
      expect(useSessionStore.getState().agents.find((row) => row.id === "a")?.name).toBe("Read name");
      expect(pinnedInOrder(useSessionStore.getState().agents).map((row) => row.id)).toEqual(["b", "a"]);
    });
    store.confirmAgentOrder(["b", "a"], write);
    expect(pinnedInOrder(useSessionStore.getState().agents).map((row) => row.id)).toEqual(["b", "a"]);
  });
});

it.each(["account", "service", "ended"])("discards %s request receipts", async (change) => {
  const store = useSessionStore.getState();
  const request = store.beginAgentRequest();
  try {
    if (change === "account") useSessionStore.setState({ accountGeneration: 2 });
    else if (change === "service") useSessionStore.setState({ dispatchGeneration: "service-b" });
    else store.endAgentRequest(request);
    store.applyAgentSnapshot([agent("old", "Obsolete")], request);
    expect(useSessionStore.getState().agents.map((row) => row.id)).toEqual(["a", "b"]);
  } finally { store.endAgentRequest(request); }
});
