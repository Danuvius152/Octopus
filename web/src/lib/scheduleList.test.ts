import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ScheduleList } from "./scheduleList";
import { useSessionStore, type Schedule } from "../stores/sessionStore";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const row = (id = "old", enabled = true) => ({ id, name: id, enabled } as Schedule);
const flush = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };
let list: ScheduleList;
let calls: { init?: RequestInit; reply: ReturnType<typeof deferred<Response>> }[];
beforeEach(() => {
  vi.useFakeTimers();
  useSessionStore.setState({ token: "token", accountGeneration: useSessionStore.getState().accountGeneration + 1, dispatchGeneration: "service" });
  calls = [];
  vi.stubGlobal("fetch", vi.fn((_url: string, init?: RequestInit) => {
    const reply = deferred<Response>(); calls.push({ init, reply }); return reply.promise;
  }));
  list = new ScheduleList();
});
afterEach(() => { list.dispose(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it("does not refresh another account's unused domain when its service identity becomes known", async () => {
  const first = list.refresh(); calls[0].reply.resolve(Response.json([row()])); await first;
  useSessionStore.getState().setToken("another-account");
  useSessionStore.setState({ dispatchGeneration: "another-service" });
  await flush(); expect(calls).toHaveLength(1);
  const entered = list.refresh(); expect(calls).toHaveLength(2);
  calls[1].reply.resolve(Response.json([row("new")])); await entered;
  expect(useSessionStore.getState().schedules.map((item) => item.id)).toEqual(["new"]);
});

it("shares reads and coalesces repeated invalidations through successive conflicts", async () => {
  useSessionStore.setState({ schedules: [row()] });
  const first = list.refresh(); expect(list.refresh()).toBe(first);
  list.changed(); list.changed(); await flush(); expect(calls).toHaveLength(1);
  calls[0].reply.resolve(Response.json([row()])); expect(await first).toEqual({ state: "pending" }); await flush();
  list.changed(); calls[1].reply.resolve(Response.json([row()])); await flush(); expect(calls).toHaveLength(3);
  calls[2].reply.resolve(Response.json([row("old", false)])); await flush();
  expect(useSessionStore.getState().schedules[0].enabled).toBe(false);
  expect(useSessionStore.getState().scheduleRead.stale).toBe(false); expect(calls).toHaveLength(3);
});

it("keeps reading during long creation and never appends its captured old list", async () => {
  useSessionStore.setState({ schedules: [row()] });
  const create = list.createFromText({ agent_id: "agent", session_id: "origin", timezone: "Asia/Shanghai", text: "later" });
  const read = list.refresh(); calls[1].reply.resolve(Response.json([])); await read;
  expect(useSessionStore.getState().schedules).toEqual([]);
  calls[0].reply.resolve(Response.json(row("new"))); expect((await create).id).toBe("new"); await flush();
  expect(useSessionStore.getState().schedules).toEqual([]);
  calls[2].reply.resolve(Response.json([row("new")])); await flush();
  expect(useSessionStore.getState().schedules.map((item) => item.id)).toEqual(["new"]);
  expect(JSON.parse(String(calls[0].init?.body))).toEqual({ session_id: "origin", timezone: "Asia/Shanghai", text: "later" });
});

it("distinguishes successful creation from a failed confirmation and never retries the write", async () => {
  const create = list.createFromText({ agent_id: "agent", session_id: "origin", timezone: "UTC", text: "later" });
  calls[0].reply.resolve(Response.json(row("new"))); expect((await create).id).toBe("new"); await flush();
  calls[1].reply.reject(new Error("offline")); await flush();
  expect(useSessionStore.getState().schedules[0].id).toBe("new");
  expect(useSessionStore.getState().scheduleRead.error).toBe("offline");
  await vi.advanceTimersByTimeAsync(60_000); expect(calls).toHaveLength(2);
});

it("read failure ends dirty work and a later notification can restart it", async () => {
  const first = list.refresh(); list.changed();
  calls[0].reply.reject(new Error("offline")); expect(await first).toMatchObject({ state: "failed" }); await flush();
  expect(calls).toHaveLength(1); list.changed(); await flush(); expect(calls).toHaveLength(2);
});

it("applies the 30 second deadline to body decoding and rejects late bodies", async () => {
  const body = deferred<unknown>(); const first = list.refresh();
  calls[0].reply.resolve({ ok: true, json: () => body.promise } as Response); await flush();
  await vi.advanceTimersByTimeAsync(30_000); expect(await first).toMatchObject({ state: "failed" });
  const next = list.refresh(); body.resolve([row("late")]); await flush();
  expect(useSessionStore.getState().schedules).toEqual([]);
  expect(useSessionStore.getState().scheduleRead.loading).toBe(true);
  calls[1].reply.resolve(Response.json([])); await next;
});

it.each(["account", "service", "socket"])("retires decoded reads and hints on %s changes", async (kind) => {
  const body = deferred<unknown>(); const read = list.refresh(); const current = list.currentGuard();
  calls[0].reply.resolve({ ok: true, json: () => body.promise } as Response); await flush();
  if (kind === "account") useSessionStore.setState({ accountGeneration: useSessionStore.getState().accountGeneration + 1 });
  if (kind === "service") useSessionStore.setState({ dispatchGeneration: "new-service" });
  if (kind === "socket") { const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration }; list.connect(source); list.disconnect(source); }
  expect(await read).toEqual({ state: "obsolete" }); expect(current()).toBe(false);
  body.resolve([row("late")]); await flush(); expect(useSessionStore.getState().schedules).toEqual([]);
});

it.each([409, 503])("confirms HTTP %i write errors without replaying writes", async (status) => {
  const write = list.setEnabled("old", false).catch((error: unknown) => error);
  calls[0].reply.resolve(Response.json({ detail: "cannot save" }, { status }));
  expect(await write).toMatchObject({ outcome: status === 409 ? "refused" : "unknown" }); await flush();
  expect(calls.filter((call) => call.init?.method === "PATCH")).toHaveLength(1);
  expect(calls).toHaveLength(2);
});
