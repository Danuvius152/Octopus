import { useState } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ArchivedSessionsDialog } from "./ArchivedSessionsDialog";
import { useSessionStore, type Agent, type SessionInfo } from "../stores/sessionStore";
import { deferred } from "../test-support/deferred";
import { sessions } from "../lib/sessionList";

const archived = { id: "archived", agent_id: "owner", name: "Archived conversation", archived: true, status: "idle" } as SessionInfo;
let responses: ReturnType<typeof deferred<Response>>[];

beforeEach(() => {
  responses = [];
  useSessionStore.setState({ ...useSessionStore.getInitialState(), accountGeneration: useSessionStore.getState().accountGeneration + 1 });
  useSessionStore.getState().setToken("archive-account");
  sessionStorage.clear();
  useSessionStore.setState({ identity: { user_id: `archive-user-${useSessionStore.getState().accountGeneration}`, label: "Archive user", is_admin: false },
    agents: [{ id: "owner", name: "Owner" } as Agent], dispatchGeneration: "service" });
});
afterEach(async () => {
  cleanup();
  useSessionStore.getState().setToken("");
  await act(async () => { for (const response of responses) response.reject(new Error("Test request retired")); });
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function ArchivePage() {
  const [open, setOpen] = useState(true);
  return <><button onClick={() => setOpen(true)}>Open archive</button>
    <ArchivedSessionsDialog open={open} onOpenChange={setOpen} /></>;
}

it.each([false, true])("confirms a permanent delete and can retry post-commit cleanup=%s", async (pendingCleanup) => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValue(true);
  let writes = 0;
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    if (init?.method === "DELETE") {
      expect(String(url)).toMatch(/\/archived\?archived_only=true$/);
      writes++;
      if (writes === 1) throw new TypeError("Lost DELETE response");
      return new Response(null, { status: 204 });
    }
    if (String(url).endsWith("/presence")) {
      reads++;
      return Response.json({ archived: null, deletion_pending: pendingCleanup && writes === 1 });
    }
    return Response.json(writes > 0 ? [] : [archived]);
  }));
  render(<ArchivePage />);
  act(() => useSessionStore.setState({ activeSessionId: archived.id }));
  const remove = await screen.findByRole("button", { name: "Delete" });
  fireEvent.click(remove);
  expect(writes).toBe(0);
  fireEvent.click(remove);
  expect(confirm).toHaveBeenLastCalledWith(expect.stringContaining("Permanently delete"));
  await waitFor(() => expect(screen.queryByText(archived.name, { exact: true })).toBeNull());
  expect(useSessionStore.getState().activeSessionId).toBeNull();
  expect(reads).toBe(1);
  if (pendingCleanup) {
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByRole("button", { name: "Open archive" }));
    fireEvent.click(await screen.findByRole("button", { name: "Retry deletion" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Retry deletion" })).toBeNull());
    expect(writes).toBe(2);
    expect(confirm).toHaveBeenCalledTimes(2);
  } else {
    expect(screen.queryByRole("button", { name: "Retry deletion" })).toBeNull();
  }
});

it("deletes only the confirmed groups and keeps restored and retryable outcomes distinct", async () => {
  const other = { ...archived, id: "other", agent_id: "second", origin: "schedule", name: "Archived schedule run" };
  const restored = { ...archived, id: "restored", name: "Restored history" };
  const cleanupPending = { ...archived, id: "pending", agent_id: "unknown", name: "Cleanup pending" };
  const newcomer = { ...archived, id: "new", name: "New archive" };
  let rows = [cleanupPending, restored, archived, other];
  let deletionPending = false;
  const firstReply = deferred<Response>();
  responses.push(firstReply);
  const attempts: string[] = [];
  useSessionStore.setState(state => ({ agents: [...state.agents, { id: "second", name: "Second agent" } as Agent] }));
  const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValue(true);
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    const path = new URL(String(url));
    if (init?.method === "DELETE") {
      expect(path.searchParams.get("archived_only")).toBe("true");
      const id = path.pathname.split("/").at(-1)!;
      attempts.push(id);
      if (id === restored.id && rows.some(row => row.id === id && !row.archived)) return new Response(null, { status: 404 });
      rows = rows.filter(row => row.id !== id);
      if (id === cleanupPending.id && !deletionPending) {
        deletionPending = true;
        return firstReply.promise;
      }
      if (id === cleanupPending.id) deletionPending = false;
      return new Response(null, { status: 204 });
    }
    if (path.pathname.endsWith(`/${cleanupPending.id}/presence`)) {
      return Response.json({ archived: null, deletion_pending: deletionPending });
    }
    if (path.pathname.endsWith(`/${restored.id}/presence`)) {
      return Response.json({ archived: false, deletion_pending: false });
    }
    return Response.json(rows);
  }));
  render(<ArchivePage />);
  await screen.findByText("Unknown agent");
  expect(screen.getByText("Second agent")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Delete all" }));
  expect(attempts).toEqual([]);
  fireEvent.click(screen.getByRole("button", { name: "Delete all" }));
  await waitFor(() => expect(attempts).toContain(cleanupPending.id));
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  fireEvent.click(screen.getByRole("button", { name: "Open archive" }));
  expect(await screen.findByRole("button", { name: "Deleting all…" })).toBeDisabled();
  await act(async () => {
    rows = [newcomer, ...rows.map(row => row.id === restored.id ? { ...row, archived: false } : row)];
    firstReply.resolve(new Response(null, { status: 503 }));
  });
  expect(await screen.findByText(newcomer.name)).toBeVisible();
  expect(await screen.findByRole("status")).toHaveTextContent("1 session was restored");
  expect(screen.queryByText(archived.name, { exact: true })).toBeNull();
  expect(screen.queryByText(other.name, { exact: true })).toBeNull();
  expect(screen.queryByText(cleanupPending.name, { exact: true })).toBeNull();
  expect(screen.getByRole("alert")).toHaveTextContent(cleanupPending.name);
  expect(new Set(attempts)).toEqual(new Set([cleanupPending.id, restored.id, archived.id, other.id]));
  fireEvent.click(screen.getByRole("button", { name: "Retry deletion" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "Retry deletion" })).toBeNull());
  expect(attempts.filter(id => id === cleanupPending.id)).toHaveLength(2);
  expect(confirm).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  fireEvent.click(screen.getByRole("button", { name: "Open archive" }));
  expect(await screen.findByRole("status")).toHaveTextContent("1 session was restored");
});

it("counts a restored target confirmed by a later list read after the batch ended", async () => {
  const second = { ...archived, id: "second", name: "Second restored history" };
  const firstRefresh = deferred<Response>();
  const secondPresence = deferred<Response>();
  const followupRefresh = deferred<Response>();
  responses.push(firstRefresh, secondPresence, followupRefresh);
  const livePresence = { archived: false, deletion_pending: false };
  const restoredRows = [{ ...archived, archived: false }, { ...second, archived: false }];
  let lists = 0;
  let secondPresenceStarted = false;
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    if (init?.method === "DELETE") return new Response(null, { status: 404 });
    if (String(url).endsWith(`/${archived.id}/presence`)) return Response.json(livePresence);
    if (String(url).endsWith(`/${second.id}/presence`)) {
      secondPresenceStarted = true;
      return secondPresence.promise;
    }
    lists++;
    if (lists === 1) return Response.json([archived, second]);
    if (lists === 2) return firstRefresh.promise;
    if (lists === 3) return followupRefresh.promise;
    return Response.json(restoredRows);
  }));

  render(<ArchivePage />);
  fireEvent.click(await screen.findByRole("button", { name: "Delete all" }));
  await waitFor(() => expect(secondPresenceStarted).toBe(true));
  await act(async () => firstRefresh.resolve(Response.json([{ ...archived, archived: false }, second])));
  expect(screen.queryByText(archived.name, { exact: true })).toBeNull();
  await act(async () => secondPresence.resolve(Response.json(livePresence)));
  expect(await screen.findByRole("status")).toHaveTextContent("1 session was restored");
  expect(screen.getByRole("button", { name: "Retry deletion" })).toBeVisible();

  await act(async () => followupRefresh.resolve(Response.json(restoredRows)));
  expect(screen.queryByRole("button", { name: "Retry deletion" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  fireEvent.click(screen.getByRole("button", { name: "Open archive" }));
  expect(await screen.findByRole("status")).toHaveTextContent("2 sessions were restored");
});

it.each([false, true])("shows separate batch and single-delete results when the batch settles on retry=%s", async (retry) => {
  const newest = { ...archived, id: "newest", name: "Independent archive" };
  const refreshAfterSingle = deferred<Response>();
  responses.push(refreshAfterSingle);
  let rows = [archived];
  let holdRefresh = false;
  let batchAttempted = false;
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    const path = new URL(String(url)).pathname;
    if (init?.method === "DELETE") {
      const id = path.split("/").at(-1);
      if (id === archived.id && !batchAttempted) {
        batchAttempted = true;
        return new Response(null, { status: 503 });
      }
      rows = rows.map(row => row.id === id ? { ...row, archived: false } : row);
      holdRefresh = id === newest.id;
      return new Response(null, { status: 404 });
    }
    if (path.endsWith("/presence")) {
      const id = path.split("/").at(-2);
      return Response.json({ archived: rows.find(row => row.id === id)?.archived, deletion_pending: false });
    }
    if (holdRefresh) {
      holdRefresh = false;
      return refreshAfterSingle.promise;
    }
    return Response.json(rows);
  }));
  render(<ArchivePage />);
  fireEvent.click(await screen.findByRole("button", { name: "Delete all" }));
  await screen.findByRole("button", { name: "Retry deletion" });
  await act(async () => {
    rows = [archived, newest];
    await sessions.refresh(true);
  });
  const independentRow = screen.getByText(newest.name).closest<HTMLElement>(".archived-session-row")!;
  fireEvent.click(within(independentRow).getByRole("button", { name: "Delete" }));
  const independentNotice = `"${newest.name}" was restored and has been kept.`;
  expect(await screen.findByText(independentNotice)).toHaveRole("status");
  await act(async () => {
    if (!retry) rows = rows.map(row => ({ ...row, archived: false }));
    refreshAfterSingle.resolve(Response.json(rows));
  });
  if (retry) fireEvent.click(screen.getByRole("button", { name: "Retry deletion" }));
  expect(await screen.findByText("1 session was restored and has been kept.")).toHaveRole("status");
  expect(screen.getByText(independentNotice)).toBeVisible();
  expect(screen.queryByText(`"${archived.name}" was restored and has been kept.`)).toBeNull();
  expect(screen.queryByRole("button", { name: "Retry deletion" })).toBeNull();
});

it("clears the old single-delete notice when a new batch confirms deletion of a re-archived session", async () => {
  let row: SessionInfo | null = archived;
  let writes = 0;
  vi.spyOn(window, "confirm").mockReturnValueOnce(true).mockReturnValueOnce(false).mockReturnValueOnce(true);
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    if (init?.method === "DELETE") {
      writes++;
      if (writes === 1) {
        row = { ...archived, archived: false };
        return new Response(null, { status: 404 });
      }
      row = null;
      return new Response(null, { status: 204 });
    }
    if (String(url).endsWith("/presence")) {
      return Response.json({ archived: row?.archived ?? null, deletion_pending: false });
    }
    return Response.json(row ? [row] : []);
  }));
  render(<ArchivePage />);
  fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
  const notice = `"${archived.name}" was restored and has been kept.`;
  expect(await screen.findByText(notice)).toBeVisible();
  await screen.findByText(/No archived sessions/);
  await act(async () => {
    row = archived;
    await sessions.refresh(true);
  });

  const deleteAll = screen.getByRole("button", { name: "Delete all" });
  fireEvent.click(deleteAll);
  expect(writes).toBe(1);
  expect(screen.getByText(notice)).toBeVisible();
  fireEvent.click(deleteAll);
  expect(await screen.findByText(/No archived sessions/)).toBeVisible();
  expect(writes).toBe(2);
  expect(screen.queryByText(notice)).toBeNull();
});

it("keeps a confirmed batch across reopening and stops queued writes on account change", async () => {
  const queued = { ...archived, id: "queued", name: "Queued deletion" };
  const replacement = { ...archived, id: "replacement", name: "Other account archive" };
  const originalReply = deferred<Response>();
  const replacementReply = deferred<Response>();
  responses.push(originalReply, replacementReply);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  const writes: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    const newAccount = new Headers(init?.headers).get("Authorization") === "Bearer replacement-account";
    if (init?.method === "DELETE") {
      writes.push(`${newAccount ? "replacement" : "original"}:${new URL(String(url)).pathname.split("/").at(-1)}`);
      return newAccount ? replacementReply.promise : originalReply.promise;
    }
    return Response.json(newAccount ? [replacement] : [archived, queued]);
  }));
  render(<ArchivePage />);
  fireEvent.click(await screen.findByRole("button", { name: "Delete all" }));
  await waitFor(() => expect(writes).toEqual([`original:${archived.id}`]));
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  fireEvent.click(screen.getByRole("button", { name: "Open archive" }));
  expect(await screen.findByRole("button", { name: "Deleting all…" })).toBeDisabled();

  act(() => useSessionStore.getState().setToken("replacement-account"));
  fireEvent.click(await screen.findByRole("button", { name: "Delete all" }));
  await waitFor(() => expect(writes).toEqual([`original:${archived.id}`, `replacement:${replacement.id}`]));
  await act(async () => originalReply.resolve(new Response(null, { status: 204 })));
  expect(screen.getByRole("button", { name: "Deleting all…" })).toBeDisabled();
  await act(async () => replacementReply.resolve(new Response(null, { status: 204 })));
  expect(await screen.findByText(/No archived sessions/)).toBeVisible();
  expect(writes).toEqual([`original:${archived.id}`, `replacement:${replacement.id}`]);
  expect(confirm).toHaveBeenCalledTimes(2);
});

it("keeps one pending restoration across closing and reopening the archive", async () => {
  const pending = deferred<Response>();
  responses.push(pending);
  const writes: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    if (init?.method === "POST") { writes.push(init.body); return pending.promise; }
    if (String(url).endsWith("/restoration")) return Response.json({ service_generation: "service", operation_id: null, state: "idle" });
    return Response.json([archived]);
  }));
  render(<ArchivePage />);
  fireEvent.click(await screen.findByRole("button", { name: "Unarchive" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  fireEvent.click(screen.getByRole("button", { name: "Open archive" }));
  fireEvent.click(await screen.findByRole("button", { name: "Unarchive" }));
  expect(writes).toHaveLength(1);
});

it.each(["current", "reopen", "view-return", "agent-session", "account", "service", "connection"])(
  "keeps archive mutations separate from the original global opening: %s", async (transition) => {
  const receipt = deferred<Response>();
  responses.push(receipt);
  let restored = false;
  let operation: { operation_id: string; service_generation: string } | undefined;
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    if (init?.method === "POST") { operation = JSON.parse(init.body as string); return receipt.promise; }
    if (String(url).endsWith("/restoration")) return Response.json({ service_generation: "service", state: "idle" });
    if (String(url).endsWith("/unrelated/presence")) return Response.json({ archived: false, deletion_pending: false });
    return Response.json(useSessionStore.getState().token === "archive-account" ? [{ ...archived, archived: !restored }] : []);
  }));
  render(<ArchivePage />);
  fireEvent.click(await screen.findByRole("button", { name: "Unarchive" }));
  await waitFor(() => expect(operation).toBeDefined());
  if (transition === "reopen") {
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByRole("button", { name: "Open archive" }));
  } else if (transition === "view-return") {
    act(() => {
      useSessionStore.getState().openManage("connectors");
      useSessionStore.getState().showChat();
    });
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open archive" }));
  } else if (transition === "agent-session") {
    act(() => useSessionStore.setState({ activeAgentId: "unrelated", activeSessionId: "unrelated" }));
  } else if (transition === "account") {
    act(() => useSessionStore.getState().setToken("foreign-account"));
  } else if (transition === "service") {
    act(() => useSessionStore.setState({ dispatchGeneration: "replacement-service" }));
  } else if (transition === "connection") {
    act(() => sessions.connect({ socket: {}, accountGeneration: useSessionStore.getState().accountGeneration }));
  }
  expect(screen.getByRole("dialog")).toBeVisible();
  await act(async () => { restored = true; receipt.resolve(Response.json({ ...operation, state: "finished" })); });
  if (transition === "account") {
    expect(useSessionStore.getState().sessions).toEqual([]);
  } else {
    await waitFor(() => expect(useSessionStore.getState().sessions).toEqual([{ ...archived, archived: false }]));
  }
  if (["reopen", "view-return", "account", "service", "connection"].includes(transition)) {
    expect(screen.getByRole("dialog")).toBeVisible();
    expect(useSessionStore.getState().activeSessionId).not.toBe(archived.id);
  } else {
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    if (["current", "agent-session"].includes(transition)) expect(useSessionStore.getState().activeSessionId).toBe(archived.id);
    else expect(useSessionStore.getState().activeSessionId).not.toBe(archived.id);
  }
});

it("ignores an old failed restore after a new opening is shown", async () => {
  const receipt = deferred<Response>();
  responses.push(receipt);
  let posted = false;
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    if (init?.method === "POST") { posted = true; return receipt.promise; }
    if (String(url).endsWith("/restoration")) return Response.json({ service_generation: "service", state: "idle" });
    return Response.json([archived]);
  }));
  render(<ArchivePage />);
  fireEvent.click(await screen.findByRole("button", { name: "Unarchive" }));
  await waitFor(() => expect(posted).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  fireEvent.click(screen.getByRole("button", { name: "Open archive" }));
  await act(async () => receipt.resolve(Response.json({ detail: "Old refusal" }, { status: 503 })));
  expect(screen.getByRole("dialog")).toBeVisible();
  expect(screen.getByRole("button", { name: "Unarchive" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Confirm restoration" })).toBeEnabled();
  expect(screen.queryByText("Old refusal")).toBeNull();
  expect(useSessionStore.getState().sessions).toEqual([]);
});
