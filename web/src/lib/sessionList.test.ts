import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SessionList } from "./sessionList";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const row = (id = "old", status = "running") => ({ id, name: id, status, archived: false } as SessionInfo);
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
let list: SessionList;
let calls: ReturnType<typeof deferred<Response>>[];
beforeEach(() => {
  vi.useFakeTimers();
  useSessionStore.getState().setToken(`account-${useSessionStore.getState().accountGeneration}`);
  useSessionStore.setState({ dispatchGeneration: "service" });
  calls = [];
  vi.stubGlobal("fetch", vi.fn(() => { const reply = deferred<Response>(); calls.push(reply); return reply.promise; }));
  list = new SessionList();
  list.row(row());
});
afterEach(() => { list.dispose(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it.each([false, true])("updates a session in place in the archived=%s list", (archived) => {
  for (const id of ["first", "middle", "last"]) list.row({ ...row(id), archived });
  const key = archived ? "archivedSessions" : "sessions";
  const before = useSessionStore.getState()[key].map((item) => item.id);

  expect(list.row({ ...row("middle", "idle"), name: "Updated", archived })).toBe(true);

  const after = useSessionStore.getState()[key];
  expect(after.map((item) => item.id)).toEqual(before);
  expect(after.find((item) => item.id === "middle")).toMatchObject({ name: "Updated", status: "idle" });
});

it.each(["archive", "delete"])("does not let a stale snapshot resurrect a session after %s", async (action) => {
  const old = list.refresh();
  if (action === "archive") list.archive("old");
  else list.remove("old");
  calls[0].resolve(Response.json([row()]));
  expect(await old).toEqual({ state: "pending" }); await flush();
  expect(useSessionStore.getState().sessions).toEqual([]);
  expect(calls).toHaveLength(2);
  calls[1].resolve(Response.json([])); await flush();
  expect(useSessionStore.getState().sessions).toEqual([]);
  expect(useSessionStore.getState().archivedSessions.map((item) => item.id)).toEqual(action === "archive" ? ["old"] : []);
  expect(useSessionStore.getState().sessionRead.stale).toBe(false);
});

it("preserves sessions created during a read and coalesces conflicting observations", async () => {
  const old = list.refresh();
  const create = list.capture();
  expect(await list.created(row("created", "idle"), create)).toBe(true);
  list.patch("old", { status: "idle" });
  expect(list.refresh()).toBe(old);
  calls[0].resolve(Response.json([row()])); await old; await flush();
  expect(useSessionStore.getState().sessions.map((item) => item.id)).toEqual(["old", "created"]);
  expect(calls).toHaveLength(2);
  calls[1].resolve(Response.json([row("old", "idle"), row("created", "idle")])); await flush();
  expect(useSessionStore.getState().sessionRead.stale).toBe(false);
});

it("rejects an old detail without overriding the newer status and confirms through one list read", async () => {
  const detail = list.capture();
  list.patch("old", { status: "idle" });
  expect(list.row(row(), detail)).toBe(false);
  await flush();
  expect(useSessionStore.getState().sessions[0].status).toBe("idle");
  expect(calls).toHaveLength(1);
  calls[0].resolve(Response.json([row("old", "idle")])); await flush();
});

it("rejects reversed reconnect responses and retired sockets without extra reads", async () => {
  const accountGeneration = useSessionStore.getState().accountGeneration;
  const first = { socket: {}, accountGeneration }, second = { socket: {}, accountGeneration };
  list.connect(first); await flush();
  const detail = list.capture();
  list.disconnect(first); list.connect(second); await flush();
  expect(list.accepts(first)).toBe(false);
  calls[1].resolve(Response.json([row("new", "idle")])); await flush();
  calls[0].resolve(Response.json([row()])); await flush();
  expect(list.row(row(), detail)).toBe(false); await flush();
  expect(useSessionStore.getState().sessions.map((item) => item.id)).toEqual(["new"]);
  expect(calls).toHaveLength(2);
});

it.each(["account", "token", "service", "dispose"])("retires reads and details across %s changes", async (kind) => {
  const old = list.refresh(), detail = list.capture();
  if (kind === "account") useSessionStore.setState({ accountGeneration: useSessionStore.getState().accountGeneration + 1 });
  if (kind === "token") useSessionStore.getState().setToken("new-token");
  if (kind === "service") useSessionStore.setState({ dispatchGeneration: "new-service" });
  if (kind === "dispose") list.dispose();
  expect(await old).toEqual({ state: "obsolete" });
  expect(list.row(row("late"), detail)).toBe(false);
  calls[0].resolve(Response.json([row("late")])); await flush();
  expect(useSessionStore.getState().sessions.some((item) => item.id === "late")).toBe(false);
});

it("preserves rows and exposes errors until a successful explicit refresh", async () => {
  const old = list.refresh();
  calls[0].reject(new Error("offline"));
  expect(await old).toMatchObject({ state: "failed" }); await flush();
  expect(useSessionStore.getState().sessions[0].id).toBe("old");
  expect(useSessionStore.getState().sessionRead).toMatchObject({ stale: true, error: "offline" });
  await vi.advanceTimersByTimeAsync(60_000); expect(calls).toHaveLength(1);
  const refresh = list.refresh(); calls[1].resolve(Response.json([])); await refresh;
  expect(useSessionStore.getState().sessionRead).toMatchObject({ loaded: true, stale: false, error: null });
});

it.each(["created", "patch", "restored", "archive", "remove"] as const)("confirms a late %s write through the current connection", async (kind) => {
  const original = list.capture();
  list.connect({ socket: {}, accountGeneration: useSessionStore.getState().accountGeneration }); await flush();
  calls[0].resolve(Response.json([row("old", "idle")])); await flush();
  const applied = kind === "created" ? await list.created(row("late"), original) :
    kind === "restored" ? await list.restored(row("late"), original) :
    kind === "patch" ? list.patch("old", { status: "running" }, original) : list[kind]("old", original);
  expect(applied).toBe(false); await flush();
  expect(useSessionStore.getState().sessions).toEqual([row("old", "idle")]);
  expect(calls).toHaveLength(2);
  calls[1].resolve(Response.json([row("confirmed", "idle")])); await flush();
  expect(useSessionStore.getState().sessions).toEqual([row("confirmed", "idle")]);
});

it.each(["account", "token", "dispose"])("does not confirm an obsolete write after %s changes", async (kind) => {
  const original = list.capture();
  if (kind === "account") useSessionStore.setState({ accountGeneration: useSessionStore.getState().accountGeneration + 1 });
  if (kind === "token") useSessionStore.getState().setToken("other-token");
  if (kind === "dispose") list.dispose();
  expect(await list.created(row("late"), original)).toBe(false);
  expect(list.patch("old", { status: "idle" }, original)).toBe(false);
  await flush(); expect(calls).toHaveLength(0);
});

it.each(["created", "restored"] as const)("a %s receipt can follow an already observed live identity without replacing newer fields", async (kind) => {
  const original = list.capture();
  list.row(row("new", "idle"));
  expect(await list[kind](row("new", "running"), original)).toBe(true);
  expect(useSessionStore.getState().sessions.find((item) => item.id === "new")?.status).toBe("idle");
});

it.each(["created", "restored"] as const)("waits for one accepted snapshot before allowing navigation after %s", async (kind) => {
  const oldRead = list.refresh(), original = list.capture();
  list.patch("old", { status: "idle" });
  const confirmed = list[kind](row("new", "running"), original);
  calls[0].resolve(Response.json([row()]));
  expect(await oldRead).toEqual({ state: "pending" }); await flush();
  expect(calls).toHaveLength(2);
  list.patch("old", { status: "running" });
  calls[1].resolve(Response.json([row("new", "running")])); await flush();
  expect(calls).toHaveLength(3);
  expect(useSessionStore.getState().sessions.some((item) => item.id === "new")).toBe(false);
  calls[2].resolve(Response.json([row("new", "idle")]));
  expect(await confirmed).toBe(true);
  expect(useSessionStore.getState().sessions).toEqual([row("new", "idle")]);
});

it.each(["created", "restored"] as const)("ends %s confirmation on read failure or scope retirement", async (kind) => {
  const original = list.capture();
  list.patch("old", { status: "idle" });
  const failed = list[kind](row("new"), original);
  calls[0].reject(new Error("offline"));
  expect(await failed).toBe(false);
  expect(useSessionStore.getState().sessionRead).toMatchObject({ stale: true, error: "offline" });
  const next = list.capture();
  list.patch("old", { status: "running" });
  const retired = list[kind](row("new"), next);
  useSessionStore.getState().setToken("different-account");
  expect(await retired).toBe(false);
  calls[1].resolve(Response.json([row("new")])); await flush();
  expect(useSessionStore.getState().sessions).toEqual([]);
  expect(calls).toHaveLength(2);
});

it("confirms concurrent creation receipts after the read begun before their last invalidation", async () => {
  const original = list.capture();
  list.patch("old", { status: "idle" });
  const first = list.created(row("one"), original);
  const second = list.created(row("two"), original);
  let confirmed = 0;
  void first.then(() => confirmed++); void second.then(() => confirmed++);
  calls[0].resolve(Response.json([row("one")])); await flush();
  expect(confirmed).toBe(0);
  expect(calls).toHaveLength(2);
  calls[1].resolve(Response.json([row("one"), row("two")]));
  expect(await Promise.all([first, second])).toEqual([true, true]);
  expect(calls).toHaveLength(2);
});
