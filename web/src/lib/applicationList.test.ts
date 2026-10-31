import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApplicationList, orderedApplicationPins } from "./applicationList";
import { useSessionStore, type Application } from "../stores/sessionStore";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const row = (id = "a", patch: Partial<Application> = {}): Application => ({
  id, name: id, description: "app", icon: null, agent_id: "agent", session_id: null,
  app_dir: `/apps/${id}`, entrypoint: "index.html", status: "building", error: null,
  archived: false, pinned: true, pin_order: id === "a" ? 1 : 2,
  created_at: "now", updated_at: "now", last_built_at: null, ...patch,
});
const flush = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };
let list: ApplicationList;
let calls: { url: string; init?: RequestInit; reply: ReturnType<typeof deferred<Response>> }[];
beforeEach(() => {
  vi.useFakeTimers();
  useSessionStore.setState({ token: "token", accountGeneration: useSessionStore.getState().accountGeneration + 1,
    dispatchGeneration: "service-a", sessions: [], messages: {}, unseenFailedApplications: [],
    activeApplicationId: null, mainView: "chat" });
  calls = [];
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    const reply = deferred<Response>(); calls.push({ url, init, reply }); return reply.promise;
  }));
  list = new ApplicationList();
});
afterEach(() => { list.dispose(); vi.useRealTimers(); vi.unstubAllGlobals(); });
const rows = () => useSessionStore.getState().applications;

describe("application list observations", () => {
  it("does not carry an old account's active list subscriptions into another account", async () => {
    const initial = list.refresh(); calls[0].reply.resolve(json([row()])); await initial;
    useSessionStore.getState().setToken("another-account");
    useSessionStore.setState({ dispatchGeneration: "another-service" });
    await flush(); expect(calls).toHaveLength(1);
    const entered = list.refresh(); expect(calls).toHaveLength(2);
    calls[1].reply.resolve(json([row("new-account")])); await entered;
    expect(rows().map((item) => item.id)).toEqual(["new-account"]);
  });

  it("shares one read, coalesces pushes, and keeps reading after a second conflict", async () => {
    const first = list.refresh();
    expect(list.refresh()).toBe(first);
    list.receive({ type: "application_updated", application: row("a", { status: "ready" }) });
    list.receive({ type: "application_updated", application: row("a", { status: "ready" }) });
    await flush(); expect(calls).toHaveLength(1);
    calls[0].reply.resolve(json([row()]));
    expect(await first).toEqual({ state: "pending" });
    await flush(); expect(calls).toHaveLength(2); expect(rows()[0].status).toBe("ready");
    list.receive({ type: "application_updated", application: row("b") });
    calls[1].reply.resolve(json([row()]));
    await flush(); expect(calls).toHaveLength(3);
    calls[2].reply.resolve(json([row("a", { status: "ready" }), row("b")]));
    await flush(); expect(rows().map((item) => item.id)).toEqual(["a", "b"]);
    expect(useSessionStore.getState().applicationReads.live.stale).toBe(false);
    expect(calls).toHaveLength(3);
  });

  it("confirms a late building event after a ready snapshot, preserving optional runtime fields", async () => {
    const ready = row("a", { status: "ready" });
    const read = list.refresh(); calls[0].reply.resolve(json([ready])); await read;
    list.receive({ type: "application_updated", application: row() });
    expect(rows()[0].status).toBe("building");
    expect(rows()[0]).not.toHaveProperty("backend");
    await flush(); calls[1].reply.resolve(json([ready])); await flush();
    expect(rows()[0].status).toBe("ready");
  });

  it("gives both application queries turns while live pushes continue", async () => {
    void list.refresh(); void list.refresh("archived");
    for (let i = 0; i < 4; i++) {
      list.receive({ type: "application_updated", application: row() });
      calls[i].reply.resolve(json([])); await flush();
    }
    expect(calls.slice(0, 4).map((call) => call.url.includes("archived=true"))).toEqual([false, true, false, true]);
  });

  it("does not restore an archived row from a read begun before restore", async () => {
    useSessionStore.setState({ archivedApplications: [row("a", { archived: true })] });
    const old = list.refresh("archived");
    const restore = list.restore("a");
    calls[1].reply.resolve(json(row())); await restore;
    expect(rows().map((item) => item.id)).toEqual(["a"]);
    calls[0].reply.resolve(json([row("a", { archived: true })]));
    expect(await old).toEqual({ state: "pending" }); await flush();
    expect(useSessionStore.getState().archivedApplications).toEqual([]);
    calls[2].reply.resolve(json([row()])); await flush();
    calls[3].reply.resolve(json([])); await flush();
    expect(rows()).toHaveLength(1); expect(useSessionStore.getState().archivedApplications).toEqual([]);
  });

  it.each(["application_archived", "application_deleted"])("%s cannot be undone by a read and clears selection and failures", async (type) => {
    useSessionStore.setState({ applications: [row()], activeApplicationId: "a", mainView: "application", unseenFailedApplications: ["a"] });
    const old = list.refresh();
    list.receive({ type, application_id: "a" });
    expect(useSessionStore.getState()).toMatchObject({ applications: [], activeApplicationId: null, mainView: "chat", unseenFailedApplications: [] });
    calls[0].reply.resolve(json([row()])); await old; await flush(); expect(rows()).toEqual([]);
  });

  it("times out body decoding, ignores the late body and waits for a new trigger", async () => {
    const body = deferred<unknown>();
    const first = list.refresh();
    calls[0].reply.resolve({ ok: true, json: () => body.promise } as Response); await flush();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(await first).toMatchObject({ state: "failed", message: expect.stringContaining("30 seconds") });
    expect(calls[0].init?.signal?.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(60_000); expect(calls).toHaveLength(1);
    const next = list.refresh(); body.resolve([row("old")]); await flush();
    expect(rows()).toEqual([]); expect(useSessionStore.getState().applicationReads.live.loading).toBe(true);
    calls[1].reply.resolve(json([row("new")])); await next;
    expect(rows()[0].id).toBe("new");
  });

  it("a failed read preserves rows, stops dirty work and permits a later event", async () => {
    useSessionStore.setState({ applications: [row()] });
    const read = list.refresh(); void list.refresh("archived");
    list.receive({ type: "application_updated", application: row() });
    calls[0].reply.reject(new Error("offline"));
    expect(await read).toMatchObject({ state: "failed" }); await flush();
    expect(calls).toHaveLength(1); expect(rows()).toHaveLength(1);
    list.receive({ type: "application_updated", application: row() }); await flush();
    expect(calls).toHaveLength(2);
  });

  it.each(["account", "service", "socket"])("invalidates old decoded responses and callbacks on %s changes", async (kind) => {
    const body = deferred<unknown>();
    const old = list.refresh(); const current = list.currentGuard();
    calls[0].reply.resolve({ ok: true, json: () => body.promise } as Response); await flush();
    if (kind === "account") useSessionStore.setState({ accountGeneration: useSessionStore.getState().accountGeneration + 1 });
    if (kind === "service") useSessionStore.setState({ dispatchGeneration: "service-b" });
    if (kind === "socket") {
      const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
      list.connect(source); list.disconnect(source);
    }
    expect(await old).toEqual({ state: "obsolete" }); expect(current()).toBe(false);
    body.resolve([row("old")]); await flush(); expect(rows()).toEqual([]);
  });

  it("rejects replaced socket events and hydration whose body finishes after disconnect", async () => {
    const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
    list.connect(source); await flush();
    list.receive({ type: "application_updated", application: row("a", { session_id: "build" }) }, source);
    const detail = calls.find((call) => call.url.endsWith("/api/sessions/build/info"))!;
    const body = deferred<unknown>(); detail.reply.resolve({ ok: true, json: () => body.promise } as Response); await flush();
    list.disconnect(source);
    list.connect({ ...source, socket: {} });
    list.receive({ type: "application_updated", application: row("stale", { status: "failed", session_id: "old-build" }) }, source);
    body.resolve({ id: "build", messages: [], next_message_seq: 1, pending_questions: [],
      dispatch_state: { generation: "service-a", revision: 1, paused: false, active_request_id: null, items: [] } });
    await flush();
    expect(rows().map((item) => item.id)).toEqual(["a"]);
    expect(useSessionStore.getState().sessions).toEqual([]);
    expect(useSessionStore.getState().unseenFailedApplications).toEqual([]);
    expect(calls.some((call) => call.url.endsWith("old-build"))).toBe(false);
  });
});

describe("application writes and ordering", () => {
  beforeEach(() => useSessionStore.setState({ applications: [row(), row("b")] }));
  it("reads during long writes without treating a snapshot as post-write confirmation", async () => {
    const build = list.build("a", "change");
    const read = list.refresh(); calls[1].reply.resolve(json([row("a", { status: "ready" })])); await read;
    calls[0].reply.resolve(json(row("a", { status: "building" }))); await build; await flush();
    expect(rows()[0].status).toBe("ready"); expect(calls).toHaveLength(3);
    calls[2].reply.reject(new Error("confirmation offline")); await flush();
    expect(useSessionStore.getState().applicationReads.live.error).toContain("confirmation offline");
    expect(calls.filter((call) => call.init?.method === "POST")).toHaveLength(1);
  });

  it("keeps pushed status and removals under ID-only optimistic ordering and rejects competing pin", async () => {
    const order = list.reorder(["b", "a"]);
    await expect(list.setPinned("a", false)).rejects.toMatchObject({ outcome: "refused" });
    expect(calls).toHaveLength(1);
    list.receive({ type: "application_updated", application: row("b", { status: "ready" }) });
    list.receive({ type: "application_deleted", application_id: "a" });
    expect(orderedApplicationPins(rows(), useSessionStore.getState().applicationPinOrder).map((item) => [item.id, item.status])).toEqual([["b", "ready"]]);
    calls[0].reply.resolve(json([row(), row("b")])); await order; await flush();
    expect(rows().map((item) => item.id)).toEqual(["b"]);
    expect(useSessionStore.getState().applicationPinOrder).toEqual(["b"]);
    const confirm = calls.at(-1)!; confirm.reply.reject(new Error("no confirmation")); await flush();
    expect(useSessionStore.getState().applicationPinOrder).toBeNull();
    expect(useSessionStore.getState().applicationOrdering).toBe(false);
  });

  it("an older confirmation cannot clear a newer order intent", async () => {
    const first = list.reorder(["b", "a"]);
    calls[0].reply.resolve(json([row(), row("b")])); await first; await flush();
    const oldConfirm = calls[1];
    const second = list.reorder(["a", "b"]);
    oldConfirm.reply.resolve(json([row(), row("b")])); await flush();
    expect(useSessionStore.getState().applicationPinOrder).toEqual(["a", "b"]);
    expect(useSessionStore.getState().applicationOrdering).toBe(true);
    calls[2].reply.resolve(json([row(), row("b")])); await second; await flush();
    expect(useSessionStore.getState().applicationOrdering).toBe(false);
  });

  it.each([409, 500])("drops optimistic order on HTTP %i without restoring old rows or replaying", async (status) => {
    const result = list.reorder(["b", "a"]).catch((error: unknown) => error);
    list.receive({ type: "application_deleted", application_id: "a" });
    calls[0].reply.resolve(json({ detail: "rejected" }, status));
    expect(await result).toMatchObject({ outcome: status === 409 ? "refused" : "unknown" }); await flush();
    expect(rows().map((item) => item.id)).toEqual(["b"]);
    expect(useSessionStore.getState().applicationPinOrder).toBeNull();
    expect(calls.filter((call) => call.init?.method === "PUT")).toHaveLength(1);
    expect(useSessionStore.getState().applicationOrdering).toBe(false);
  });
});
