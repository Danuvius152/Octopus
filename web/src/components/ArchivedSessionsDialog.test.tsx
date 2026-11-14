import { useState } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  vi.unstubAllGlobals();
});

function ArchivePage() {
  const [open, setOpen] = useState(true);
  return <><button onClick={() => setOpen(true)}>Open archive</button>
    <ArchivedSessionsDialog open={open} onOpenChange={setOpen} /></>;
}

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
