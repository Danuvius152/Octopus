import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { handleWsMessage } from "../hooks/useWebSocket";
import { BgTaskChip } from "../components/BgTaskChip";
import { SessionAuxiliaryNotice } from "../components/SessionAuxiliaryNotice";
import { useSessionStore, type BgTask, type SessionInfo } from "../stores/sessionStore";
import { deferred } from "../test-support/deferred";
import { loadBgTaskSnapshot, observeSession } from "./hydrateSession";
import { sessions } from "./sessionList";

const running = { id: "cold-task", session_id: "cold-session", command: "echo complete", description: "Cold task", status: "running",
  stdout: "", stderr: "", started_at: "now", exit_code: null } as BgTask;
const completed = { ...running, status: "completed", exit_code: 0, completed_at: "later" } as BgTask;
const reads: ReturnType<typeof deferred<Response>>[] = [];
const start = () => handleWsMessage({ type: "bg_started", session_id: running.session_id, task_id: running.id,
  command: running.command, started_at: running.started_at });
const completion = () => handleWsMessage({ type: "bg_completed", session_id: running.session_id, task_id: running.id,
  status: "completed", exit_code: 0, completed_at: "later" });
const delivery = () => handleWsMessage({ type: "bg_delivery", session_id: running.session_id, task_id: running.id,
  delivery_disposition: "unknown", delivery_error: "Return not confirmed" });
const card = () => render(<><SessionAuxiliaryNotice sessionId={running.session_id} /><BgTaskChip sessionId={running.session_id} taskId={running.id} /></>);
beforeEach(() => {
  useSessionStore.getState().setToken("");
  useSessionStore.getState().setToken("cold-token");
  useSessionStore.setState({ dispatchGeneration: "cold-service", bgTasks: {}, sessions: [{ id: running.session_id }] as SessionInfo[] });
  reads.length = 0;
  vi.stubGlobal("fetch", vi.fn(() => { const read = deferred<Response>(); reads.push(read); return read.promise; }));
});
afterEach(() => { cleanup(); useSessionStore.getState().setToken(""); vi.unstubAllGlobals(); });

it("keeps the terminal card and both output streams across delayed start and completion events", async () => {
  const list = loadBgTaskSnapshot(running.session_id);
  const result = { ...completed, stdout: "stdout first line\nstdout last line", stderr: "stderr first line\nstderr last line", truncated: true };
  await act(async () => { reads[0].resolve(Response.json([result])); await list; });
  card();
  fireEvent.click(screen.getByRole("button", { name: "Expand bg task" }));

  const expectResult = () => {
    expect(screen.getByText("bg · completed")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
    expect(screen.getByText(/stdout first line/)).toHaveTextContent("stdout first line stdout last line");
    expect(screen.getByText(/stderr first line/)).toHaveTextContent("stderr first line stderr last line");
    expect(screen.getByText("(truncated)")).toBeVisible();
  };
  expectResult();
  act(start);
  expectResult();
  act(() => { completion(); });
  expectResult();
  await act(async () => reads[1].resolve(Response.json(result)));
});

it.each([false, true])("fills omitted terminal fields and streams from a detail read (newer running list=%s)", async (overlap) => {
  start();
  card();
  fireEvent.click(screen.getByRole("button", { name: "Expand bg task" }));
  const list = overlap ? loadBgTaskSnapshot(running.session_id) : undefined;
  act(() => handleWsMessage({ type: "bg_completed", session_id: running.session_id, task_id: running.id,
    status: "failed", completed_at: "later", delivery_disposition: "unknown", delivery_error: "Not an execution field" }));
  expect(screen.getByText("bg · failed")).toBeVisible();
  expect(screen.queryByText("Not an execution field")).toBeNull();
  if (list) await act(async () => { reads[1].resolve(Response.json([{ ...running, truncated: false, stop_reason: null }])); await list; });

  await act(async () => reads[0].resolve(Response.json({ ...completed, status: "failed", exit_code: 2,
    truncated: true, stop_reason: "exit_nonzero", stdout: "Saved stdout", stderr: "Saved stderr" })));
  for (const text of ["exit 2", "(truncated)", "The command exited unsuccessfully", "Saved stdout", "Saved stderr"]) {
    expect(screen.getByText(text)).toBeVisible();
  }
});

it.each([
  { patch: { delivery_error: null }, expected: { delivery_disposition: "stopped", delivery_error: null } },
  { patch: { delivery_disposition: null }, expected: { delivery_disposition: null, delivery_error: "HTTP diagnosis" } },
] as const)("protects only the delivery field explicitly observed in $patch", async ({ patch, expected }) => {
  const original = loadBgTaskSnapshot(running.session_id);
  reads[0].resolve(Response.json([{ ...completed, delivery_disposition: "unknown", delivery_error: "Old diagnosis" }]));
  await original;
  card();
  fireEvent.click(screen.getByRole("button", { name: "Expand bg task" }));
  act(() => handleWsMessage({ type: "bg_delivery", session_id: running.session_id, task_id: running.id, ...patch }));
  act(() => { completion(); });
  await act(async () => reads[1].resolve(Response.json({ ...completed, delivery_disposition: "stopped", delivery_error: "HTTP diagnosis" })));
  expect(useSessionStore.getState().bgTasks[running.session_id][0]).toMatchObject(expected);
  expect(screen.queryByText("Old diagnosis")).toBeNull();
  if (expected.delivery_disposition) expect(screen.getByText("Automatic return stopped")).toBeVisible();
  else expect(screen.queryByText("Automatic return stopped")).toBeNull();
});

it("keeps a task confirmed by detail when a late list only covers newer tasks", async () => {
  const initial = loadBgTaskSnapshot(running.session_id);
  reads[0].resolve(Response.json([completed]));
  await initial;
  const list = loadBgTaskSnapshot(running.session_id);
  card();
  fireEvent.click(screen.getByRole("button", { name: "Expand bg task" }));
  await act(async () => reads[2].resolve(Response.json({ ...completed, stdout: "Retained stdout", stderr: "Retained stderr" })));
  expect(screen.getByText("Retained stdout")).toBeVisible();

  const recent = Array.from({ length: 200 }, (_, i) => ({ ...completed, id: `recent-${i}`, started_at: "more recent" }));
  await act(async () => { reads[1].resolve(Response.json(recent)); await list; });
  for (const text of ["bg · completed", "Retained stdout", "Retained stderr"]) {
    expect(screen.getByText(text)).toBeVisible();
  }
});

it.each([true, false])("confirms an unknown completion without letting the cold list regress it (detailFirst=%s)", async detailFirst => {
  const list = loadBgTaskSnapshot(running.session_id);
  completion();
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(String(vi.mocked(fetch).mock.calls[1][0])).toContain(`/bg-tasks/${running.id}`);
  if (detailFirst) {
    await act(async () => reads[1].resolve(Response.json(completed)));
    card();
    await act(async () => { reads[0].resolve(Response.json([running])); await list; });
  } else {
    await act(async () => { reads[0].resolve(Response.json([running])); await list; });
    card();
    await act(async () => reads[1].resolve(Response.json(completed)));
  }
  expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  expect(useSessionStore.getState().bgTasks[running.session_id][0]).toMatchObject({ status: "completed", exit_code: 0 });
});

it.each([
  [true, false], [false, false], [true, true], [false, true],
])("retains cold completion demand across the first service confirmation (listFirst=%s, deliveryPending=%s)", async (listFirst, deliveryPending) => {
  useSessionStore.setState({ dispatchGeneration: null });
  const detail = deferred<Response>(), list = deferred<Response>();
  const taskReads: ReturnType<typeof deferred<Response>>[] = [];
  vi.stubGlobal("fetch", vi.fn((url: unknown) => {
    const path = String(url);
    if (path.endsWith("?view=browser")) return detail.promise;
    if (path.endsWith("/bg-tasks")) return list.promise;
    if (path.endsWith(`/bg-tasks/${running.id}`)) {
      const read = deferred<Response>(); taskReads.push(read); return read.promise;
    }
    return Promise.resolve(Response.json([]));
  }));
  const session = observeSession(running.session_id);
  completion();
  if (deliveryPending) delivery();
  await act(async () => {
    detail.resolve(Response.json({ id: running.session_id, messages: [], active_attempt: null,
      dispatch_state: { generation: "cold-service", revision: 0, paused: false, active_request_id: null, active_operation: null, items: [] } }));
    await session.done;
  });
  expect(useSessionStore.getState().dispatchGeneration).toBe("cold-service");
  if (listFirst) await act(async () => list.resolve(Response.json([running])));
  await act(async () => taskReads[0].resolve(Response.json(completed)));
  // The pre-generation detail is not evidence for the newly confirmed service.
  expect(taskReads).toHaveLength(2);
  if (deliveryPending) { delivery(); delivery(); }
  await act(async () => taskReads[1].resolve(Response.json(completed)));
  card();
  if (deliveryPending) {
    expect(taskReads).toHaveLength(3);
    await act(async () => taskReads[2].resolve(Response.json({ ...completed, delivery_disposition: "unknown", delivery_error: "Return not confirmed" })));
    expect(screen.getByText("Return not confirmed")).toBeVisible();
  }
  if (!listFirst) await act(async () => list.resolve(Response.json([running])));
  expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  expect(useSessionStore.getState().bgTasks[running.session_id][0]).toMatchObject({ status: "completed", exit_code: 0 });
  expect(screen.queryByRole("alert")).toBeNull();
  session.cancel();
});

it.each([true, false])("retains a delivery confirmation demand while detail is pending (coldListFirst=%s)", async coldListFirst => {
  const list = loadBgTaskSnapshot(running.session_id);
  completion();
  if (coldListFirst) await act(async () => { reads[0].resolve(Response.json([running])); await list; });
  delivery();
  delivery();
  expect(reads).toHaveLength(2); // Repeated events share the pending read.
  await act(async () => reads[1].resolve(Response.json(completed)));
  expect(reads).toHaveLength(3); // A read begun before delivery cannot confirm it.
  card();
  await act(async () => reads[2].resolve(Response.json({ ...completed, delivery_disposition: "unknown", delivery_error: "Return not confirmed" })));
  if (!coldListFirst) await act(async () => { reads[0].resolve(Response.json([running])); await list; });
  expect(screen.getByText("Return admission unconfirmed")).toBeVisible();
  expect(screen.getByText("Return not confirmed")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  expect(reads).toHaveLength(3);
});

it("shows a failed unknown-event read and recovers through the existing auxiliary Retry", async () => {
  const list = loadBgTaskSnapshot(running.session_id);
  delivery();
  await act(async () => reads[1].resolve(Response.json({ detail: "offline" }, { status: 503 })));
  await act(async () => { reads[0].resolve(Response.json([running])); await list; });
  card();
  expect(screen.getByRole("alert")).toHaveTextContent("Could not refresh background task details.");
  fireEvent.click(screen.getByRole("button", { name: "Retry background task details" }));
  expect(String(vi.mocked(fetch).mock.calls[2][0])).toContain(`/bg-tasks/${running.id}`);
  await act(async () => reads[2].resolve(Response.json({ ...completed, delivery_disposition: "unknown", delivery_error: "Return not confirmed" })));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByText("Return not confirmed")).toBeVisible();
});

it.each(["account", "deletion", "service", "initial-service-retirement", "disconnect"])("retires the original read and its follow-up demand on %s", async reason => {
  const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
  if (reason === "disconnect") sessions.connect(source);
  if (reason === "initial-service-retirement") useSessionStore.setState({ dispatchGeneration: null });
  completion();
  delivery();
  act(() => {
    if (reason === "account") useSessionStore.getState().setToken("another-token");
    else if (reason === "deletion") handleWsMessage({ type: "session_deleted", session_id: running.session_id });
    else if (reason === "disconnect") sessions.disconnect(source);
    else {
      if (reason === "initial-service-retirement") useSessionStore.setState({ dispatchGeneration: "cold-service" });
      useSessionStore.setState({ dispatchGeneration: "next-service" });
    }
  });
  await act(async () => reads[0].resolve(Response.json({ ...completed, delivery_error: "Obsolete error" })));
  expect(reads).toHaveLength(1);
  expect(useSessionStore.getState().bgTasks[running.session_id]).toBeUndefined();
  expect(useSessionStore.getState().sessionAuxiliaryErrors[running.session_id]).toBeUndefined();
  expect(useSessionStore.getState().bgTaskReadFailures[running.session_id]).toBeUndefined();
});

it.each(["account", "deletion", "service", "disconnect"])("retires a published task Retry on %s", async (reason) => {
  const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
  if (reason === "disconnect") sessions.connect(source);
  completion();
  await act(async () => reads[0].resolve(new Response(null, { status: 503 })));
  card();
  expect(screen.getByRole("button", { name: "Retry background task details" })).toBeVisible();

  act(() => {
    if (reason === "account") useSessionStore.getState().setToken("another-token");
    else if (reason === "deletion") handleWsMessage({ type: "session_deleted", session_id: running.session_id });
    else if (reason === "disconnect") sessions.disconnect(source);
    else useSessionStore.getState().applyDispatchState(running.session_id,
      { generation: "next-service", revision: 0, paused: false, items: [], active_request_id: null, active_operation: null });
  });
  expect(screen.queryByRole("alert")).toBeNull();
});

it("patches a known task without starting a redundant confirmation", () => {
  useSessionStore.getState().upsertBgTask(running.session_id, running);
  card();
  act(() => { completion(); delivery(); });
  expect(reads).toHaveLength(0);
  expect(screen.getByText("Return not confirmed")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
});
