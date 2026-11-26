import { deferred } from "../test-support/deferred";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SessionList } from "./sessionList";
import { ArchiveDeadline } from "./sessionArchive";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";

const row = (id = "old", status = "running") => ({ id, name: id, status, archived: false } as SessionInfo);
const absentPresence = () => Response.json({ archived: null, deletion_pending: false });
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
  const old = list.refresh(true);
  if (action === "archive") list.archive("old");
  else list.remove("old");
  calls[0].resolve(Response.json([row()]));
  expect(await old).toEqual({ state: "pending" }); await flush();
  expect(useSessionStore.getState().sessions).toEqual([]);
  expect(calls).toHaveLength(2);
  calls[1].resolve(Response.json(action === "archive" ? [{ ...row(), archived: true }] : [])); await flush();
  expect(useSessionStore.getState().sessions).toEqual([]);
  expect(useSessionStore.getState().archivedSessions.map((item) => item.id)).toEqual(action === "archive" ? ["old"] : []);
  expect(useSessionStore.getState().sessionRead.stale).toBe(false);
});

it("keeps a sparse successor relationship through a read failure and lets a newer deletion retire it", async () => {
  list.archive("old", undefined, "new");
  expect(useSessionStore.getState().sessionSuccessors).toEqual({ old: "new" });
  expect(useSessionStore.getState().sessions).toEqual([]);
  calls[0].reject(new Error("offline"));
  await flush();
  expect(useSessionStore.getState().sessionRead.error).toBe("offline");
  expect(useSessionStore.getState().sessionSuccessors).toEqual({ old: "new" });

  list.archive("old", undefined, undefined, undefined, { model: null });
  expect(calls).toHaveLength(2);
  const retry = list.refresh(true);
  list.remove("new");
  const archived = { ...row(), archived: true };
  calls[1].resolve(Response.json([archived, row("new")]));
  expect(await retry).toEqual({ state: "pending" });
  await flush();
  calls[2].resolve(Response.json([archived]));
  await flush();
  expect(useSessionStore.getState().sessionSuccessors).toEqual({});
  expect(useSessionStore.getState().sessions).toEqual([]);
  expect(useSessionStore.getState().archivedSessions).toEqual([archived]);
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

it("coalesces creation announcements without summaries into a current list read", async () => {
  const stale = list.refresh();
  list.announced("one");
  list.announced("two");
  calls[0].resolve(Response.json([row()]));
  expect(await stale).toEqual({ state: "pending" });
  await flush();
  expect(calls).toHaveLength(2);
  calls[1].resolve(Response.json([row(), row("one"), row("two")]));
  await flush();
  expect(useSessionStore.getState().sessions.map((session) => session.id)).toEqual(["old", "one", "two"]);
  list.announced("one");
  await flush();
  expect(calls).toHaveLength(2);
});

it.each(["success", "failure"])("keeps newer creation demand when the in-flight read fails, then handles %s", async (outcome) => {
  const old = list.refresh();
  list.announced("one");
  list.announced("two");
  calls[0].resolve(new Response(null, { status: 503 }));
  expect(await old).toMatchObject({ state: "failed" });
  await flush();
  expect(calls).toHaveLength(2);

  const response = outcome === "success" ? Response.json([row(), row("one"), row("two")]) :
    new Response(null, { status: 503 });
  calls[1].resolve(response);
  await flush();
  expect(useSessionStore.getState().sessions.map((session) => session.id)).toEqual(
    outcome === "success" ? ["old", "one", "two"] : ["old"]);
  expect(useSessionStore.getState().sessionRead.error).toEqual(outcome === "success" ? null : expect.stringContaining("503"));
  await vi.advanceTimersByTimeAsync(60_000);
  expect(calls).toHaveLength(2);
});

it.each(["delete", "inclusive read"])("cannot fill a missing archived row after a newer %s", async (boundary) => {
  const historical = list.observe("historical");
  if (boundary === "delete") list.remove("historical");
  else {
    const inclusive = list.refresh(true);
    calls[0].resolve(Response.json([row()]));
    expect(await inclusive).toEqual({ state: "applied" });
  }
  expect(list.metadata({ ...row("historical", "idle"), archived: true }, historical.observation)).toBe(false);
  expect(useSessionStore.getState().archivedSessions).toEqual([]);
  historical.finish();
});

it("keeps another consumer's GET alive while confirming an unknown archive from a later snapshot", async () => {
  const reading = list.refresh(true);
  const signal = vi.mocked(fetch).mock.calls[0][1]!.signal!;
  const tracked = list.observe("old", useSessionStore.getState(), "archive");
  const confirming = list.confirmArchive("old", tracked.observation);
  expect(signal.aborted).toBe(false);
  expect(calls).toHaveLength(1);
  calls[0].resolve(Response.json([row()]));
  expect(await reading).toEqual({ state: "pending" });
  await flush();
  expect(calls).toHaveLength(2);
  calls[1].resolve(Response.json([{ ...row(), archived: true }]));
  expect(await confirming).toEqual({ archived: true, successor: null });
  tracked.finish();
});

it("replaces a pre-confirmation projection even while its unrelated presence check is hung", async () => {
  const archived = { ...row(), archived: true };
  const other = row("other");
  list.row(other);
  const reading = list.refresh(true);
  calls[0].resolve(Response.json([archived])); await flush();
  const oldProbe = vi.mocked(fetch).mock.calls[1][1]!.signal!;
  await vi.advanceTimersByTimeAsync(1_000);
  const tracked = list.observe("old", useSessionStore.getState(), "archive");
  const confirming = list.confirmArchive("old", tracked.observation);
  const settled = vi.fn();
  void confirming.then(settled);
  await flush();
  expect(settled).not.toHaveBeenCalled();
  expect(calls).toHaveLength(3);
  expect(oldProbe.aborted).toBe(true);
  expect(await reading).toEqual({ state: "pending" }); await flush();
  await vi.advanceTimersByTimeAsync(29_000);
  expect(settled).not.toHaveBeenCalled();
  calls[2].resolve(Response.json([archived, other]));
  expect(await confirming).toEqual({ archived: true, successor: null });
  calls[1].resolve(absentPresence()); await flush();
  expect(useSessionStore.getState().sessions).toEqual([other]);
  expect(useSessionStore.getState().sessionRead.error).toBeNull();
  tracked.finish();
});

it.each(["reconnect", "reconnect with link", "new service", "new successor", "restored", "ended"] as const)(
  "confirms a known archive receipt after %s only within its original authority", async (boundary) => {
    const original = list.observe("old", useSessionStore.getState(), "archive");
    const receipt = { successorId: "child", original: original.observation };
    const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
    const old = { ...row(), archived: true };
    const child = row("child");
    const replacement = row("replacement");
    if (boundary.startsWith("reconnect")) list.connect(source);
    if (boundary === "reconnect with link") {
      list.row(child);
      list.archive("old", undefined, child.id);
    }
    if (boundary === "new service") useSessionStore.setState({ dispatchGeneration: "replacement-service" });
    if (boundary === "new successor") {
      list.row(replacement);
      list.archive("old", undefined, replacement.id);
    }
    if (boundary === "ended") original.finish();
    const manual = list.observe("old", useSessionStore.getState(), "archive");
    const confirming = list.confirmArchive("old", manual.observation, undefined, receipt);
    calls[0].resolve(Response.json(boundary === "restored" ? [row(), child] : [old, child, replacement]));
    expect(await confirming).toEqual({ archived: boundary !== "restored", successor: boundary === "reconnect with link" ? child : null });
    manual.finish();
    if (boundary === "restored") {
      const after = list.observe("old", useSessionStore.getState(), "archive");
      const retry = list.confirmArchive("old", after.observation, undefined, receipt);
      calls[1].resolve(Response.json([old, child]));
      expect(await retry).toEqual({ archived: true, successor: null });
      after.finish();
    }
    expect(useSessionStore.getState().sessionSuccessors.old).toBe(boundary === "reconnect with link" ? child.id :
      boundary === "new successor" ? replacement.id : undefined);
    original.finish();
  },
);

it("shares the original confirmation deadline across stale snapshots and overlapping waiters without aborting the shared GET", async () => {
  const observation = list.observe("old", useSessionStore.getState(), "archive");
  const budget = new ArchiveDeadline();
  const first = list.confirmArchive("old", observation.observation, budget);
  const firstSignal = vi.mocked(fetch).mock.calls[0][1]!.signal!;
  await vi.advanceTimersByTimeAsync(10_000);
  list.patch("old", { name: "Changed while reading" });
  calls[0].resolve(Response.json([row()])); await flush();
  expect(firstSignal.aborted).toBe(true); // Its own transport completed.
  expect(calls).toHaveLength(2);
  await vi.advanceTimersByTimeAsync(10_000);
  const overlap = list.confirmArchive("old", observation.observation, budget);
  const shared = list.refresh(true);
  const sharedSignal = vi.mocked(fetch).mock.calls[1][1]!.signal!;
  await vi.advanceTimersByTimeAsync(9_999);
  expect(sharedSignal.aborted).toBe(false);
  await vi.advanceTimersByTimeAsync(1);
  expect(await first).toBeNull();
  expect(await overlap).toBeNull();
  expect(sharedSignal.aborted).toBe(false);
  expect(calls).toHaveLength(2);
  calls[1].resolve(Response.json([{ ...row(), archived: true }]));
  // Overlap did not invalidate the already post-effect snapshot, and neither
  // timed-out waiter had authority to cancel another consumer's read.
  expect(await shared).toEqual({ state: "applied" });
  observation.finish();
});

it.each(["created", "successor"])("gives a later %s lookup only the original confirmation's remaining time", async (lookup) => {
  const budget = new ArchiveDeadline();
  const observation = list.observe("old", useSessionStore.getState(), "archive");
  await vi.advanceTimersByTimeAsync(20_000);
  let pending: Promise<boolean | SessionInfo | null>;
  if (lookup === "created") {
    list.patch("old", { name: "Changed since the POST" });
    pending = list.created(row("new"), observation.observation, { archivedId: "old", deadline: budget });
  } else {
    list.archive("old", undefined, "new");
    pending = list.confirmSuccessor("old", observation.observation, budget);
  }
  const signal = vi.mocked(fetch).mock.calls[0][1]!.signal!;
  let settled = false;
  void pending.then(() => { settled = true; });
  await vi.advanceTimersByTimeAsync(9_999);
  expect(settled).toBe(false);
  await vi.advanceTimersByTimeAsync(1);
  expect(await pending).toBe(lookup === "created" ? false : null);
  expect(signal.aborted).toBe(false);
  observation.finish();
});

it.each(["archived", "deleted"])("confirms a missing selected session after reconnect before treating it as %s", async (outcome) => {
  useSessionStore.setState({ activeSessionId: "old" });
  const deleted = vi.fn(); const unsubscribe = list.onDeleted(deleted);
  list.connect({ socket: {}, accountGeneration: useSessionStore.getState().accountGeneration }); await flush();
  calls[0].resolve(Response.json([])); await flush();
  expect(useSessionStore.getState().activeSessionId).toBe("old");
  expect(deleted).not.toHaveBeenCalled();
  expect(calls).toHaveLength(2);
  calls[1].resolve(Response.json(outcome === "archived" ? [{ ...row(), archived: true }] : [])); await flush();
  if (outcome === "deleted") {
    expect(useSessionStore.getState().activeSessionId).toBe("old");
    calls[2].resolve(absentPresence()); await flush();
  }
  expect(useSessionStore.getState().activeSessionId).toBe(outcome === "archived" ? "old" : null);
  expect(deleted.mock.calls).toEqual(outcome === "deleted" ? [["old", true]] : []);
  unsubscribe();
});

it.each([false, true])("confirms every missing session for draft cleanup through an inclusive=%s reconnect", async (inclusive) => {
  list.row(row("selected"));
  useSessionStore.setState({ activeSessionId: "selected", messages: { old: [{ role: "user", type: "text", content: "retained" }] } });
  const deleted = vi.fn(); list.onDeleted(deleted);
  const reading = list.refresh(inclusive);
  calls[0].resolve(Response.json([row("selected")])); await flush();
  if (!inclusive) {
    expect(deleted).not.toHaveBeenCalled();
    expect(calls).toHaveLength(2);
    calls[1].resolve(Response.json([row("selected")])); await flush();
  }
  expect(deleted).not.toHaveBeenCalled();
  calls[inclusive ? 1 : 2].resolve(absentPresence()); await reading; await flush();
  expect(deleted.mock.calls).toEqual([["old", false]]);
  expect(useSessionStore.getState().messages.old).toBeUndefined();
  expect(useSessionStore.getState().activeSessionId).toBe("selected");
});

it.each(["refresh", "reconnect"] as const)("keeps a selected archived session omitted by a %s until its delayed restoration event", async (path) => {
  const archived = { ...row(), archived: true };
  list.row(archived);
  const messages = [{ role: "user", type: "text", content: "retained" }] as const;
  useSessionStore.setState({ activeSessionId: archived.id, messages: { old: [...messages] } });
  const deleted = vi.fn();
  list.onDeleted(deleted);
  const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
  const initial = list.refresh(true);
  calls[0].resolve(Response.json([archived]));
  await initial;
  if (path === "refresh") void list.refresh();
  else { list.connect(source); await flush(); }

  calls[1].resolve(Response.json([]));
  await flush();
  expect(useSessionStore.getState()).toMatchObject({ archivedSessions: [archived], activeSessionId: archived.id, messages: { old: messages } });
  expect(String(vi.mocked(fetch).mock.calls[2][0])).toMatch(/\/old\/presence$/);
  calls[2].resolve(Response.json({ archived: false, deletion_pending: false }));
  await flush();
  expect(deleted).not.toHaveBeenCalled();
  expect(useSessionStore.getState()).toMatchObject({ archivedSessions: [archived], activeSessionId: archived.id, messages: { old: messages } });

  const live = { ...archived, archived: false };
  list.restored(live);
  expect(useSessionStore.getState()).toMatchObject({ sessions: [live], archivedSessions: [], activeSessionId: live.id, messages: { old: messages } });
  expect(deleted).not.toHaveBeenCalled();
});

it.each(["account", "connection", "service"] as const)("ignores an old absence confirmation after a %s change", async (boundary) => {
  const archived = { ...row(), archived: true };
  list.row(archived);
  useSessionStore.setState({ activeSessionId: archived.id });
  const deleted = vi.fn();
  list.onDeleted(deleted);
  const reading = list.refresh(true);
  calls[0].resolve(Response.json([])); await flush();
  expect(calls).toHaveLength(2);

  if (boundary === "account") useSessionStore.getState().setToken("different-account");
  if (boundary === "connection") list.connect({ socket: {}, accountGeneration: useSessionStore.getState().accountGeneration });
  if (boundary === "service") useSessionStore.setState({ dispatchGeneration: "replacement-service" });
  expect(await reading).toEqual({ state: "obsolete" });
  const next = list.refresh(true);
  calls[2].resolve(Response.json([archived]));
  await next;
  useSessionStore.setState({ activeSessionId: archived.id });
  calls[1].resolve(absentPresence()); await flush();
  expect(useSessionStore.getState()).toMatchObject({ archivedSessions: [archived], activeSessionId: archived.id });
  expect(deleted).not.toHaveBeenCalled();
});

it("retains membership and cached input when a missing-row confirmation fails", async () => {
  const archived = { ...row(), archived: true };
  list.row(archived);
  const messages = [{ role: "user", type: "text", content: "retained" }] as const;
  useSessionStore.setState({ activeSessionId: archived.id, messages: { old: [...messages] } });
  const read = list.refresh(true);
  calls[0].resolve(Response.json([])); await flush();
  calls[1].resolve(new Response(null, { status: 503 }));
  expect(await read).toMatchObject({ state: "failed" });
  expect(useSessionStore.getState()).toMatchObject({ archivedSessions: [archived], activeSessionId: archived.id, messages: { old: messages } });
  const retry = list.refresh(true);
  calls[2].resolve(Response.json([archived]));
  expect(await retry).toEqual({ state: "applied" });
});

it.each([false, true])("settles a deletion retry on a later list when the restored row is omitted=%s", async (omitted) => {
  const live = row(), archived = { ...live, archived: true };
  list.row(archived);
  const deletion = list.deleteArchived(archived);
  calls[0].reject(new Error("Lost DELETE response")); await flush();
  expect(String(vi.mocked(fetch).mock.calls[1][0])).toMatch(/\/old\/presence$/);
  calls[1].resolve(Response.json({ archived: true, deletion_pending: false }));
  expect(await deletion).toBe("retry"); await flush();
  const reading = list.refresh(true);
  expect(String(vi.mocked(fetch).mock.calls[2][0])).toMatch(/\/sessions\?include_archived=true$/);
  calls[2].resolve(Response.json(omitted ? [] : [live])); await flush();
  if (omitted) {
    calls[3].resolve(Response.json({ archived: false, deletion_pending: false }));
    await flush();
  }
  await reading;
  if (omitted) list.restored(live);
  expect(useSessionStore.getState().sessions).toEqual([live]);
  expect(list.deletions.getState()).toEqual({});
});

it.each(["refused", "lost"])("does not retry a %s delete after the restoration event even if presence fails", async (reply) => {
  const live = row(), archived = { ...live, archived: true };
  list.row(archived);
  const deletion = list.deleteArchived(archived);
  list.restored(live);
  if (reply === "refused") calls[0].resolve(new Response(null, { status: 404 }));
  else calls[0].reject(new Error("Lost DELETE response"));
  await flush();
  calls[1].resolve(new Response(null, { status: 503 }));
  expect(await deletion).toBe("restored");
  expect(list.deletions.getState()).toEqual({});
  expect(useSessionStore.getState().sessions).toEqual([live]);
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

it("rejects reversed reconnect responses and confirms missing members through one inclusive read", async () => {
  const accountGeneration = useSessionStore.getState().accountGeneration;
  const first = { socket: {}, accountGeneration }, second = { socket: {}, accountGeneration };
  list.connect(first); await flush();
  const detail = list.capture();
  list.disconnect(first); list.connect(second); await flush();
  expect(list.accepts(first)).toBe(false);
  calls[1].resolve(Response.json([row("new", "idle")])); await flush();
  expect(calls).toHaveLength(3);
  calls[2].resolve(Response.json([row("new", "idle")])); await flush();
  expect(calls).toHaveLength(4);
  calls[3].resolve(absentPresence()); await flush();
  calls[0].resolve(Response.json([row()])); await flush();
  expect(list.row(row(), detail)).toBe(false); await flush();
  expect(useSessionStore.getState().sessions.map((item) => item.id)).toEqual(["new"]);
  expect(calls).toHaveLength(4);
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
  const refresh = list.refresh(true); calls[1].resolve(Response.json([])); await flush();
  calls[2].resolve(absentPresence()); await refresh;
  expect(useSessionStore.getState().sessionRead).toMatchObject({ loaded: true, stale: false, error: null });
});

it.each(["created", "patch", "archive", "remove"] as const)("confirms a late %s write through the current connection", async (kind) => {
  const original = list.capture();
  list.connect({ socket: {}, accountGeneration: useSessionStore.getState().accountGeneration }); await flush();
  calls[0].resolve(Response.json([row("old", "idle")])); await flush();
  const applied = kind === "created" ? await list.created(row("late"), original) :
    kind === "patch" ? list.patch("old", { status: "running" }, original) : list[kind]("old", original);
  expect(applied).toBe(false); await flush();
  expect(useSessionStore.getState().sessions).toEqual([row("old", "idle")]);
  expect(calls).toHaveLength(2);
  calls[1].resolve(Response.json([row("old", "idle"), row("confirmed", "idle")])); await flush();
  expect(useSessionStore.getState().sessions).toEqual([row("old", "idle"), row("confirmed", "idle")]);
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

it("a creation receipt can follow an already observed live identity without replacing newer fields", async () => {
  const original = list.capture();
  list.row(row("new", "idle"));
  expect(await list.created(row("new", "running"), original)).toBe(true);
  expect(useSessionStore.getState().sessions.find((item) => item.id === "new")?.status).toBe("idle");
});

it("confirms a listed creation while an unrelated presence check is still pending", async () => {
  const original = list.capture();
  list.patch("old", { status: "idle" });
  const created = row("new", "idle");
  const confirming = list.created(created, original, { includeArchived: true });
  let accepted: boolean | undefined;
  void confirming.then(result => { accepted = result; });
  calls[0].resolve(Response.json([created])); await flush();
  expect(String(vi.mocked(fetch).mock.calls[1][0])).toMatch(/\/old\/presence$/);
  expect(accepted).toBe(true);
  expect(useSessionStore.getState().sessionRead.loading).toBe(true);
  calls[1].resolve(absentPresence()); await flush();
  expect(await confirming).toBe(true);
});

it.each([0, 11_000])("waits for a valid snapshot after creation with %i ms between list changes", async (delay) => {
  const oldRead = list.refresh(true), original = list.capture();
  list.patch("old", { status: "idle" });
  const confirmed = list.created(row("new", "running"), original);
  await vi.advanceTimersByTimeAsync(delay);
  calls[0].resolve(Response.json([row()]));
  expect(await oldRead).toEqual({ state: "pending" }); await flush();
  expect(calls).toHaveLength(2);
  await vi.advanceTimersByTimeAsync(delay);
  list.patch("old", { status: "running" });
  calls[1].resolve(Response.json([row("new", "running")])); await flush();
  expect(calls).toHaveLength(3);
  expect(useSessionStore.getState().sessions.some((item) => item.id === "new")).toBe(false);
  await vi.advanceTimersByTimeAsync(delay);
  calls[2].resolve(Response.json([row("new", "idle")])); await flush();
  const settled = list.refresh(true);
  calls[3].resolve(absentPresence());
  expect(await confirmed).toBe(true);
  expect(await settled).toEqual({ state: "applied" });
  expect(useSessionStore.getState().sessions).toEqual([row("new", "idle")]);
});

it("ends creation confirmation on read failure or scope retirement", async () => {
  const original = list.capture();
  list.patch("old", { status: "idle" });
  const failed = list.created(row("new"), original);
  calls[0].reject(new Error("offline"));
  expect(await failed).toBe(false);
  expect(useSessionStore.getState().sessionRead).toMatchObject({ stale: true, error: "offline" });
  const next = list.capture();
  list.patch("old", { status: "running" });
  const retired = list.created(row("new"), next);
  useSessionStore.getState().setToken("different-account");
  expect(await retired).toBe(false);
  calls[1].resolve(Response.json([row("new")])); await flush();
  expect(useSessionStore.getState().sessions).toEqual([]);
  expect(calls).toHaveLength(2);
});

it("uses the later archived projection when an inclusive read sees creation on both sides of archive", async () => {
  const initial = list.refresh(true);
  calls[0].resolve(Response.json([row()]));
  expect(await initial).toEqual({ state: "applied" });

  const original = list.capture();
  list.patch("old", { status: "idle" });
  const created = row("new");
  const confirming = list.created(created, original);
  const archived = { ...created, archived: true };
  calls[1].resolve(Response.json([row(), created, archived]));
  expect(await confirming).toBe(false);
  expect(useSessionStore.getState().sessions).toEqual([row()]);
  expect(useSessionStore.getState().archivedSessions).toEqual([archived]);
});

it("confirms concurrent creation receipts after the read begun before their last invalidation", async () => {
  const original = list.capture();
  list.patch("old", { status: "idle" });
  const first = list.created(row("one"), original);
  const second = list.created(row("two"), original);
  let confirmed = 0;
  void first.then(() => confirmed++); void second.then(() => confirmed++);
  calls[0].resolve(Response.json([row(), row("one")])); await flush();
  expect(confirmed).toBe(0);
  expect(calls).toHaveLength(2);
  calls[1].resolve(Response.json([row(), row("one"), row("two")]));
  expect(await Promise.all([first, second])).toEqual([true, true]);
  expect(calls).toHaveLength(2);
});
