import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApplicationList, orderedApplicationPins } from "./applicationList";
import { sidebarApplications } from "./sidebarPins";
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
    useSessionStore.setState({ applications: [row()] });
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

  it("an application deletion cannot be undone by an older read and clears selection and failures", async () => {
    useSessionStore.setState({ applications: [row()], activeApplicationId: "a", mainView: "application", unseenFailedApplications: ["a"] });
    const old = list.refresh();
    list.receive({ type: "application_deleted", application_id: "a" });
    expect(useSessionStore.getState()).toMatchObject({ applications: [], activeApplicationId: null, mainView: "chat", unseenFailedApplications: [] });
    calls[0].reply.resolve(json([row()])); await old; await flush(); expect(rows()).toEqual([]);
  });

  it("clears selection and failures once the read confirms an archive", async () => {
    useSessionStore.setState({ applications: [row()], activeApplicationId: "a", mainView: "application", unseenFailedApplications: ["a"] });
    const old = list.refresh();
    list.receive({ type: "application_archived", application_id: "a" });
    calls[0].reply.resolve(json([row()]));
    expect(await old).toEqual({ state: "pending" });
    expect(useSessionStore.getState().activeApplicationId).toBe("a");
    await flush();
    calls[1].reply.resolve(json([])); await flush();
    expect(useSessionStore.getState()).toMatchObject({ applications: [], activeApplicationId: null, mainView: "chat", unseenFailedApplications: [] });
  });

  it("retires a creation's navigation eligibility when an archive hint arrives before opening", async () => {
    const lifetime = new AbortController();
    const creation = list.create({ name: "App", description: "Brief", agent_id: "agent" }, lifetime.signal);
    calls[0].reply.resolve(json(row(), 201));
    const result = await creation;
    if (result.state !== "live") throw new Error(`Expected a live receipt, got ${result.state}`);
    expect(result.canOpen()).toBe(true);
    list.receive({ type: "application_archived", application_id: "a" });
    expect(rows().map((app) => app.id)).toEqual(["a"]);
    expect(result.canOpen()).toBe(false);
    lifetime.abort();
  });

  it.each(["restore", "delete"] as const)("keeps a confirmed %s when an older archive frame arrives", async (action) => {
    const archived = row("a", { archived: true, pinned: false });
    useSessionStore.setState({ archivedApplications: [archived] });
    if (action === "restore") {
      const restored = list.restore("a");
      calls[0].reply.resolve(json(row())); await restored;
      useSessionStore.getState().openApplication("a");
    } else {
      const removed = list.remove("a", false);
      calls[0].reply.resolve(new Response(null, { status: 204 })); await removed;
    }
    await flush();
    list.receive({ type: "application_archived", application: archived });
    expect(useSessionStore.getState()).toMatchObject({
      applications: action === "restore" ? [row()] : [], archivedApplications: [],
      activeApplicationId: action === "restore" ? "a" : null,
      mainView: action === "restore" ? "application" : "chat",
    });
    calls[1].reply.resolve(json(action === "restore" ? [row()] : [])); await flush();
    calls[2].reply.resolve(json(action === "restore" ? [row()] : [])); await flush();
    expect(rows()).toEqual(action === "restore" ? [row()] : []);
    expect(useSessionStore.getState().archivedApplications).toEqual([]);
  });

  it("confirms an archived update while observing its new builder session", async () => {
    const archived = row("a", { archived: true, session_id: "replacement" });
    useSessionStore.setState({ applications: [row()], activeApplicationId: "a", mainView: "application" });
    list.receive({ type: "application_updated", application: archived });
    expect(calls.some((call) => call.url.endsWith("/api/sessions/replacement/info"))).toBe(true);
    expect(useSessionStore.getState().activeApplicationId).toBe("a");
    await flush();
    const confirmation = calls.find((call) => call.url.endsWith("/api/applications"))!;
    confirmation.reply.resolve(json([])); await flush();
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
  });

  it.each(["failed", "ready", "archived", "deleted"] as const)("settles the first unseen build failure against a %s result", async (result) => {
    const failed = row("a", { status: "failed", pinned: false });
    list.receive({ type: "application_updated", application: failed });
    expect(useSessionStore.getState().unseenFailedApplications).toEqual([]);
    await flush();
    if (result === "archived") void list.refresh("archived");
    calls[0].reply.resolve(json(result === "failed" ? [failed] : result === "ready" ? [{ ...failed, status: "ready" }] : []));
    await flush();
    if (result === "archived") {
      calls[1].reply.resolve(json([{ ...failed, archived: true }])); await flush();
      expect(useSessionStore.getState().archivedApplications.map((app) => app.id)).toEqual(["a"]);
    }
    const state = useSessionStore.getState();
    const visible = result === "failed" ? ["a"] : [];
    expect(state.unseenFailedApplications).toEqual(visible);
    expect(sidebarApplications(state.applications, state).present.map((app) => app.id)).toEqual(visible);
  });

  it("treats creation preparation failure as an unseen failure once confirmed", async () => {
    const failed = row("a", { status: "failed" });
    list.receive({ type: "application_created", application: failed });
    await flush();
    calls[0].reply.resolve(json([{ ...failed, pinned: false }])); await flush();
    const state = useSessionStore.getState();
    expect(sidebarApplications(state.applications, state).present.map((app) => app.id)).toEqual(["a"]);
  });

  it("keeps a pending failure seen when the user opens the HTTP result before the confirming read", async () => {
    const failed = row("a", { status: "failed", pinned: false });
    list.receive({ type: "application_updated", application: failed });
    const write = list.setPinned("a", false);
    calls[0].reply.resolve(json(failed)); await write; await flush();
    expect(rows()).toEqual([failed]);
    useSessionStore.getState().openApplication("a");
    useSessionStore.getState().showChat();
    const confirmation = calls.at(-1)!;
    expect(confirmation.url).toMatch(/\/api\/applications$/);
    confirmation.reply.resolve(json([failed])); await flush();
    expect(useSessionStore.getState().unseenFailedApplications).toEqual([]);
  });

  it("does not apply a pending failure hint after replacing its socket", async () => {
    const failed = row("a", { status: "failed", pinned: false });
    const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
    list.connect(source);
    list.receive({ type: "application_updated", application: failed }, source);
    await flush();
    list.disconnect(source);
    list.connect({ ...source, socket: {} });
    await flush();
    calls[1].reply.resolve(json([failed])); await flush();
    expect(rows()).toEqual([failed]);
    expect(useSessionStore.getState().unseenFailedApplications).toEqual([]);
  });

  it("ignores failures from a socket that proves it belongs to a retired service", async () => {
    const failed = row("a", { status: "failed", pinned: false });
    const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
    list.connect(source);
    list.receive({ type: "application_updated", application: failed }, source);
    await flush();
    useSessionStore.setState({ dispatchGeneration: "service-b" });
    list.observeSocketGeneration(source, "service-a");
    list.receive({ type: "application_updated", application: failed }, source);
    await flush();
    expect(calls).toHaveLength(2);
    calls[1].reply.resolve(json([failed])); await flush();
    expect(rows()).toEqual([failed]);
    expect(useSessionStore.getState().unseenFailedApplications).toEqual([]);
  });

  it.each(["archive", "delete", "unpin"] as const)("keeps a confirmed %s when older live frames arrive", async (action) => {
    const live = row();
    useSessionStore.setState({ applications: [live], activeApplicationId: "a", mainView: "application" });
    const saved = row("a", { archived: action === "archive", pinned: action !== "unpin" });
    const write = action === "archive" ? list.archive("a") : action === "delete" ? list.remove("a", false) : list.setPinned("a", false);
    calls[0].reply.resolve(action === "delete" ? new Response(null, { status: 204 }) : json(saved));
    await write;
    await flush();

    for (const type of ["application_updated", "application_created"] as const) {
      list.receive({ type, application: live });
      expect(useSessionStore.getState()).toMatchObject({
        applications: action === "unpin" ? [saved] : [],
        archivedApplications: action === "archive" ? [saved] : [],
        activeApplicationId: action === "unpin" ? "a" : null,
      });
    }
  });

  it.each(["error", "timeout"] as const)("confirms a later archive after the in-flight list read ends in %s", async (failure) => {
    useSessionStore.setState({ applications: [row()], activeApplicationId: "a", mainView: "application", unseenFailedApplications: ["a"] });
    const old = list.refresh();
    list.receive({ type: "application_archived", application_id: "a" });
    if (failure === "timeout") await vi.advanceTimersByTimeAsync(30_000);
    else calls[0].reply.reject(new Error("offline"));
    expect(await old).toMatchObject({ state: "failed" });
    await flush();
    expect(calls).toHaveLength(2);
    expect(useSessionStore.getState().activeApplicationId).toBe("a");
    calls[1].reply.resolve(json([])); await flush();
    expect(calls).toHaveLength(2);
    expect(useSessionStore.getState()).toMatchObject({ applications: [], activeApplicationId: null, mainView: "chat", unseenFailedApplications: [] });
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

  it("a failed read preserves rows and lets another view read once", async () => {
    useSessionStore.setState({ applications: [row()] });
    const read = list.refresh(); void list.refresh("archived");
    calls[0].reply.reject(new Error("offline"));
    expect(await read).toMatchObject({ state: "failed" }); await flush();
    expect(calls).toHaveLength(2);
    expect(calls[1].url).toContain("archived=true");
    calls[1].reply.reject(new Error("archive offline")); await flush();
    expect(calls).toHaveLength(2); expect(rows()).toHaveLength(1);
    list.receive({ type: "application_updated", application: row() }); await flush();
    expect(calls).toHaveLength(3);
    expect(calls[2].url).not.toContain("archived=true");
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
    useSessionStore.setState({ applications: [row()] });
    const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
    list.connect(source); await flush();
    list.receive({ type: "application_updated", application: row("a", { session_id: "build" }) }, source);
    const detail = calls.find((call) => call.url.endsWith("/api/sessions/build/info"))!;
    const body = deferred<unknown>(); detail.reply.resolve({ ok: true, json: () => body.promise } as Response); await flush();
    list.disconnect(source);
    list.connect({ ...source, socket: {} });
    list.receive({ type: "application_updated", application: row("stale", { status: "failed", session_id: "old-build" }) }, source);
    body.resolve({ id: "build", messages: [], next_message_seq: 1, pending_questions: [],
      dispatch_state: { generation: "service-a", revision: 1, paused: false, active_request_id: null, active_operation: null, items: [] } });
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

  it.each(["success", "failure"] as const)("keeps successful sorting while an archived read finishes with %s", async (result) => {
    const live = list.refresh();
    void list.refresh("archived");
    const sorted = [row("b", { pin_order: 1 }), row("a", { pin_order: 2 })];
    const order = list.reorder(["b", "a"]);
    for (const app of sorted) list.receive({ type: "application_updated", application: app });
    calls[1].reply.resolve(json(sorted)); await order; await flush();
    expect(await live).toEqual({ state: "pending" });
    expect(calls[0].init?.signal?.aborted).toBe(true);
    expect(calls[2].url).toContain("archived=true");
    if (result === "success") calls[2].reply.resolve(json([]));
    else calls[2].reply.reject(new Error("archived read failed"));
    await flush();

    expect(calls[3].url).not.toContain("archived=true");
    expect(useSessionStore.getState().applicationPinOrder).toEqual(["b", "a"]);
    expect(orderedApplicationPins(rows(), useSessionStore.getState().applicationPinOrder).map((app) => app.id)).toEqual(["b", "a"]);
    calls[3].reply.resolve(json(sorted)); await flush();
    expect(useSessionStore.getState().applicationPinOrder).toBeNull();
    expect(orderedApplicationPins(rows(), null).map((app) => app.id)).toEqual(["b", "a"]);
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
