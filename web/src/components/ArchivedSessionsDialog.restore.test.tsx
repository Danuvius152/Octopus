import { useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ArchivedSessionsDialog } from "./ArchivedSessionsDialog";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { sessions } from "../lib/sessionList";
import { deferred } from "../test-support/deferred";
import type { components } from "../api/contracts";
import { handleWsMessage } from "../hooks/useWebSocket";

type Identity = components["schemas"]["SessionRestoreRequest"];
type Proof = components["schemas"]["SessionRestoreStatus"];
const archived = (id: string): SessionInfo => ({ id, name: `Conversation ${id}`, agent_id: "agent", archived: true,
  working_dir: "/work", status: "idle", created_at: "now", message_count: 1, transcript_integrity: "ok",
  origin: "user", backend: "codex", can_steer: true, can_fork: true, fork_is_full_copy: false,
  can_observe_context: true, can_compact: false });
let rows: SessionInfo[], user: string;
let writes: { id: string; identity: Identity; result: ReturnType<typeof deferred<Response>> }[];
let states: Map<string, Proof>;
let queries: { identity: Identity; bearer: string }[], seals: Identity[];
let lists: (() => Promise<Response>) | undefined;
let query: ((identity: Identity) => Promise<Response>) | undefined;
let heldLists: ReturnType<typeof deferred<Response>>[];
function Page() {
  const [open, setOpen] = useState(true);
  return <><button onClick={() => setOpen(true)}>Open archive</button><ArchivedSessionsDialog open={open} onOpenChange={setOpen} /></>;
}
beforeEach(() => {
  useSessionStore.getState().setToken("");
  useSessionStore.getState().setToken("T1");
  user = `restore-user-${useSessionStore.getState().accountGeneration}`;
  useSessionStore.setState({ identity: { user_id: user, label: "Restorer", is_admin: false }, dispatchGeneration: "service" });
  sessionStorage.clear();
  rows = [archived("A"), archived("B")]; writes = []; states = new Map(); queries = []; seals = []; lists = undefined; query = undefined; heldLists = [];
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    const target = new URL(String(url)), path = target.pathname;
    const id = path.split("/")[3];
    if (path.endsWith("/restoration/seal")) {
      const identity = JSON.parse(init!.body as string) as Identity;
      seals.push(identity);
      const proof = states.get(identity.operation_id) ?? { ...identity, state: "sealed" };
      states.set(identity.operation_id, proof);
      return Response.json(proof);
    }
    if (path.endsWith("/restoration")) {
      if (target.search) {
        const identity = Object.fromEntries(target.searchParams) as unknown as Identity;
        queries.push({ identity, bearer: new Headers(init?.headers).get("Authorization")! });
        return query ? query(identity) : Response.json(states.get(identity.operation_id) ?? { ...identity, state: "not_registered" });
      }
      return Response.json({ service_generation: "service", state: "idle" });
    }
    if (path.endsWith("/unarchive")) {
      const identity = JSON.parse(init!.body as string) as Identity;
      const result = deferred<Response>();
      writes.push({ id, identity, result });
      return result.promise;
    }
    if (path === "/api/sessions") return lists ? lists() : Response.json(rows);
    if (target.searchParams.get("view") === "browser") return Response.json({ ...rows.find(row => row.id === id), messages: [],
      active_attempt: null, dispatch_state: { generation: "service", revision: 0, paused: false, active_request_id: null, active_operation: null, items: [] } });
    return Response.json([]);
  }));
});
afterEach(async () => {
  cleanup();
  useSessionStore.getState().setToken("");
  await act(async () => {
    for (const response of [...writes.map(write => write.result), ...heldLists]) response.reject(new Error("Test request retired"));
  });
  vi.unstubAllGlobals();
});
const row = (id: string) => within(screen.getByText(`Conversation ${id}`).closest(".archived-session-row")! as HTMLElement);
async function click(id: string) {
  await screen.findByText(`Conversation ${id}`);
  fireEvent.click(row(id).getByRole("button", { name: "Unarchive" }));
  await waitFor(() => expect(writes.some(write => write.id === id)).toBe(true));
}
async function finish(index: number, success = true) {
  const write = writes[index];
  const proof: Proof = { ...write.identity, state: "finished", ...(success ? {} : { status_code: 404, message: "Session unavailable" }) };
  states.set(write.identity.operation_id, proof);
  if (success) rows = rows.map(item => item.id === write.id ? { ...item, archived: false } : item);
  await act(async () => write.result.resolve(Response.json(proof)));
}

it.each(["none", "service", "connection"])("keeps navigation after the first service generation unless its scope retires (%s)", async retirement => {
  useSessionStore.setState({ dispatchGeneration: null });
  render(<Page />); await click("A");
  rows = rows.map(item => item.id === "A" ? { ...item, archived: false } : item);
  act(() => handleWsMessage({ type: "session_unarchived", session_id: "A", session: rows[0],
    dispatch_state: { generation: "service", revision: 1, paused: false, active_request_id: null, active_operation: null, items: [] } }));
  if (retirement === "service") act(() => {
    for (const generation of ["next-service", "service"]) handleWsMessage({ type: "dispatch_state", session_id: "A",
      dispatch_state: { generation, revision: 1, paused: false, active_request_id: null, active_operation: null, items: [] } });
  });
  if (retirement === "connection") act(() => sessions.connect({ socket: {}, accountGeneration: useSessionStore.getState().accountGeneration }));
  await finish(0);
  expect(useSessionStore.getState().sessions.some(item => item.id === "A")).toBe(true);
  if (retirement === "none") {
    expect(useSessionStore.getState().dispatchGeneration).toBe("service");
    expect(useSessionStore.getState().activeSessionId).toBe("A");
    expect(screen.queryByRole("dialog")).toBeNull();
  } else {
    expect(useSessionStore.getState().activeSessionId).not.toBe("A");
    expect(screen.getByRole("dialog")).toBeVisible();
  }
});

it("keeps the admitted click's navigation when a duplicate click arrives before rendering disables it", async () => {
  render(<Page />);
  await screen.findByText("Conversation A");
  const button = row("A").getByRole("button", { name: "Unarchive" });
  act(() => { button.click(); button.click(); });
  await waitFor(() => expect(writes).toHaveLength(1));
  await finish(0);
  expect(useSessionStore.getState().activeSessionId).toBe("A");
  expect(screen.queryByRole("dialog")).toBeNull();
});

it.each([false, true])("restores independent targets in parallel and only the last click navigates (lastFirst=%s)", async lastFirst => {
  render(<Page />);
  await click("A");
  fireEvent.click(row("A").getByRole("button", { name: "Unarchive" }));
  expect(writes).toHaveLength(1);
  await click("B");
  expect(writes).toHaveLength(2);
  if (lastFirst) {
    await finish(1);
    expect(useSessionStore.getState().activeSessionId).toBe("B");
    await finish(0);
  } else {
    await finish(0);
    expect(screen.getByRole("dialog")).toBeVisible();
    expect(useSessionStore.getState().activeSessionId).not.toBe("A");
    await finish(1);
  }
  expect(useSessionStore.getState().activeSessionId).toBe("B");
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("never revives A's navigation after the later B click fails", async () => {
  render(<Page />);
  await click("A"); await click("B");
  await act(async () => writes[1].result.resolve(Response.json({ detail: "Unavailable" }, { status: 503 })));
  expect(screen.getByRole("alert")).toHaveTextContent("HTTP 503");
  await finish(0);
  expect(screen.getByRole("dialog")).toBeVisible();
  expect(useSessionStore.getState().activeSessionId).not.toBe("A");
});

it("shows A's confirmed refusal while the later B restoration is still pending", async () => {
  render(<Page />);
  await click("A"); await click("B");
  await finish(0, false);
  expect(row("A").getByRole("button", { name: "Unarchive" })).toBeEnabled();
  expect(row("A").getByRole("alert")).toHaveTextContent("Restoration ended (HTTP 404). Session unavailable");
  expect(row("B").getByRole("button", { name: "Unarchive" })).toBeDisabled();
  expect(row("B").queryByRole("alert")).toBeNull();
  expect(screen.getByRole("dialog")).toBeVisible();
  expect(useSessionStore.getState().activeSessionId).not.toBe("A");
  await finish(1);
  expect(useSessionStore.getState().activeSessionId).toBe("B");
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("seals an unregistered POST before releasing occupancy and ignores that POST's late reply during a successor", async () => {
  render(<Page />); await click("A");
  fireEvent.click(row("A").getByRole("button", { name: "Confirm restoration" }));
  await waitFor(() => expect(row("A").getByRole("button", { name: "Unarchive" })).toBeEnabled());
  expect(seals).toEqual([writes[0].identity]);
  expect(writes).toHaveLength(1);
  fireEvent.click(row("A").getByRole("button", { name: "Unarchive" }));
  await waitFor(() => expect(writes).toHaveLength(2));
  expect(writes[1].identity.operation_id).not.toBe(writes[0].identity.operation_id);
  await act(async () => writes[0].result.resolve(Response.json({ ...writes[0].identity, state: "sealed" }, { status: 409 })));
  expect(row("A").getByRole("button", { name: "Unarchive" })).toBeDisabled();
  expect(screen.getByRole("dialog")).toBeVisible();
  await finish(1);
  expect(useSessionStore.getState().activeSessionId).toBe("A");
});

it("keeps a running operation occupied, then confirms a finished operation whose POST response was lost", async () => {
  render(<Page />); await click("A");
  states.set(writes[0].identity.operation_id, { ...writes[0].identity, state: "running" });
  fireEvent.click(row("A").getByRole("button", { name: "Confirm restoration" }));
  await screen.findByText("Restoration is still running. Continue confirmation.");
  expect(row("A").getByRole("button", { name: "Unarchive" })).toBeDisabled();
  expect(seals).toHaveLength(0);
  states.set(writes[0].identity.operation_id, { ...writes[0].identity, state: "finished" });
  rows = rows.map(item => item.id === "A" ? { ...item, archived: false } : item);
  await act(async () => writes[0].result.reject(new Error("Lost POST response")));
  fireEvent.click(row("A").getByRole("button", { name: "Confirm restoration" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(writes).toHaveLength(1);
  expect(useSessionStore.getState().activeSessionId).toBe("A");
});

it.each([false, true])("confirms T1's unresolved identity under T2 without another restore POST (viaOtherAccount=%s)", async viaOtherAccount => {
  render(<Page />); await click("A");
  act(() => {
    if (viaOtherAccount) {
      useSessionStore.getState().setToken("TB");
      useSessionStore.getState().setIdentity({ user_id: "another-user", label: "Other", is_admin: false });
    }
    useSessionStore.getState().setToken("T2");
    useSessionStore.getState().setIdentity({ user_id: user, label: "Restorer", is_admin: false });
  });
  await screen.findByText("Conversation A");
  fireEvent.click(row("A").getByRole("button", { name: "Confirm restoration" }));
  await waitFor(() => expect(row("A").getByRole("button", { name: "Unarchive" })).toBeEnabled());
  expect(queries).toEqual([{ identity: writes[0].identity, bearer: "Bearer T2" }]);
  expect(seals).toEqual([writes[0].identity]);
  expect(writes).toHaveLength(1);
  await act(async () => writes[0].result.reject(new Error("Old T1 error")));
  expect(screen.queryByText("Old T1 error")).toBeNull();
});

it("recovers minimal persisted identity without a runtime record and never replays the old POST", async () => {
  const identity = { operation_id: "saved-before-reload", service_generation: "service" };
  sessionStorage.setItem(`octopus.session-restore.v1:${JSON.stringify([user, "A"])}`, JSON.stringify(identity));
  render(<Page />);
  await screen.findByText("Conversation A");
  expect(row("A").getByRole("button", { name: "Unarchive" })).toBeDisabled();
  fireEvent.click(row("A").getByRole("button", { name: "Confirm restoration" }));
  await waitFor(() => expect(row("A").getByRole("button", { name: "Unarchive" })).toBeEnabled());
  expect(queries[0].identity).toEqual(identity);
  expect(seals).toEqual([identity]);
  expect(writes).toHaveLength(0);
  expect(sessionStorage.getItem(`octopus.session-restore.v1:${JSON.stringify([user, "A"])}`)).toBeNull();
});

it("requires a list started after the closing proof and ignores a pre-proof archived snapshot", async () => {
  useSessionStore.setState({ archivedSessions: rows });
  const before = deferred<Response>(), after = deferred<Response>();
  heldLists.push(before, after);
  let count = 0;
  lists = () => ++count === 1 ? before.promise : after.promise;
  render(<Page />); await click("A");
  await finish(0);
  expect(count).toBe(2);
  await act(async () => before.resolve(Response.json([archived("A"), archived("B")])));
  expect(row("A").getByRole("button", { name: "Unarchive" })).toBeDisabled();
  expect(useSessionStore.getState().activeSessionId).not.toBe("A");
  await act(async () => after.resolve(Response.json(rows)));
  expect(useSessionStore.getState().activeSessionId).toBe("A");
});

it.each([404, 503, "network"] as const)("keeps unknown restoration occupied when confirmation fails: %s", async failure => {
  render(<Page />); await click("A");
  query = async () => { if (failure === "network") throw new Error("Network unavailable"); return Response.json({}, { status: failure }); };
  fireEvent.click(row("A").getByRole("button", { name: "Confirm restoration" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(failure === "network" ? "Network unavailable" : `HTTP ${failure}`));
  expect(row("A").getByRole("button", { name: "Unarchive" })).toBeDisabled();
  expect(writes).toHaveLength(1);
  expect(seals).toHaveLength(0);
});

it("keeps cross-generation uncertainty until actual retirement proof is available", async () => {
  render(<Page />); await click("A");
  query = async identity => Response.json({ operation_id: identity.operation_id, service_generation: "next-service", state: "generation_unknown" });
  fireEvent.click(row("A").getByRole("button", { name: "Confirm restoration" }));
  await screen.findByText("The previous service's retirement is unconfirmed.");
  expect(row("A").getByRole("button", { name: "Unarchive" })).toBeDisabled();
  query = async identity => Response.json({ operation_id: identity.operation_id, service_generation: "next-service", state: "generation_retired" });
  fireEvent.click(row("A").getByRole("button", { name: "Confirm restoration" }));
  await waitFor(() => expect(row("A").getByRole("button", { name: "Unarchive" })).toBeEnabled());
  expect(writes).toHaveLength(1);
});

it("retires the saved operation only on confirmed deletion and ignores its late result", async () => {
  render(<Page />); await click("A");
  rows = rows.filter(item => item.id !== "A");
  await act(async () => { await sessions.refresh(true); });
  expect(screen.queryByText("Conversation A")).toBeNull();
  expect(sessionStorage.getItem(`octopus.session-restore.v1:${JSON.stringify([user, "A"])}`)).toBeNull();
  await act(async () => writes[0].result.resolve(Response.json({ ...writes[0].identity, state: "finished" })));
  expect(useSessionStore.getState().sessions).toEqual([]);
  expect(screen.queryByText(/unconfirmed/)).toBeNull();
});

it("retries only confirmation after the finished proof's list read fails", async () => {
  render(<Page />); await click("A");
  lists = async () => Response.json({}, { status: 503 });
  await finish(0);
  expect(screen.getByText("Could not synchronize restored sessions. Retry confirmation.")).toBeVisible();
  expect(row("A").getByRole("button", { name: "Unarchive" })).toBeDisabled();
  lists = undefined;
  fireEvent.click(row("A").getByRole("button", { name: "Confirm restoration" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(writes).toHaveLength(1);
  expect(queries[0].identity).toEqual(writes[0].identity);
  expect(useSessionStore.getState().activeSessionId).toBe("A");
});

it("does not send a restore whose identity cannot be saved across reload", async () => {
  const save = Storage.prototype.setItem;
  const storage = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, name, value) {
    if (name.startsWith("octopus.session-restore.v1:")) throw new Error("Tab storage unavailable");
    return save.call(this, name, value);
  });
  render(<Page />);
  await screen.findByText("Conversation A");
  fireEvent.click(row("A").getByRole("button", { name: "Unarchive" }));
  await screen.findByText("Tab storage unavailable");
  expect(writes).toHaveLength(0);
  expect(row("A").getByRole("button", { name: "Unarchive" })).toBeEnabled();
  storage.mockRestore();
  await click("A");
  expect(writes).toHaveLength(1);
});

it("shows unreadable saved identities and recovers without sending a new restore", async () => {
  const identity = { operation_id: "saved-before-reload", service_generation: "service" };
  const savedKey = `octopus.session-restore.v1:${JSON.stringify([user, "A"])}`;
  sessionStorage.setItem(savedKey, JSON.stringify(identity));
  const get = Storage.prototype.getItem;
  const storage = vi.spyOn(Storage.prototype, "getItem").mockImplementation(function (this: Storage, name) {
    if (name === savedKey) throw new Error("Storage unavailable");
    return get.call(this, name);
  });
  render(<Page />);
  await screen.findByText("Conversation A");
  expect(screen.getByRole("alert")).toHaveTextContent("Restoration identities could not be read");
  expect(row("A").getByRole("button", { name: "Unarchive" })).toBeDisabled();
  storage.mockRestore();
  fireEvent.click(screen.getByRole("button", { name: "Retry restoration identities" }));
  fireEvent.click(row("A").getByRole("button", { name: "Confirm restoration" }));
  await waitFor(() => expect(row("A").getByRole("button", { name: "Unarchive" })).toBeEnabled());
  expect(seals).toEqual([identity]);
  expect(writes).toHaveLength(0);
});
