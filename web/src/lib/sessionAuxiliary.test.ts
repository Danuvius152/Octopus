import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { loadBgTaskSnapshot, loadResearchSnapshot, observeSessionAuxiliary } from "./hydrateSession";
import { handleWsMessage } from "../hooks/useWebSocket";
import { useSessionStore, type BgTask, type ResearchJob } from "../stores/sessionStore";

function deferred() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((yes) => { resolve = yes; });
  return { promise, resolve };
}
const bg = { id: "task", session_id: "s", status: "running" } as BgTask;
const research = { id: "task", session_id: "s", question: "A question", status: "running", phase: "search" } as ResearchJob;
beforeEach(() => {
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useSessionStore.setState({ token: "token", dispatchGeneration: "service", bgTasks: { s: [bg] }, research: { s: [research] } });
});
afterEach(() => vi.unstubAllGlobals());

it.each(["background", "research"] as const)("accepts completion across two %s snapshots sharing a running baseline", async (kind) => {
  const a = deferred(), b = deferred();
  vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => a.promise).mockImplementationOnce(() => b.promise));
  const load = kind === "background" ? loadBgTaskSnapshot : loadResearchSnapshot;
  const row = kind === "background" ? bg : research;
  const first = load("s"), second = load("s");
  a.resolve(Response.json([{ ...row }]));
  await first;
  b.resolve(Response.json([{ ...row, status: "completed" }]));
  await second;
  expect((kind === "background" ? useSessionStore.getState().bgTasks.s : useSessionStore.getState().research.s)[0].status).toBe("completed");
});

it.each(["background", "research"] as const)("keeps completed %s when a later-started running snapshot arrives last", async (kind) => {
  const a = deferred(), b = deferred();
  vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => a.promise).mockImplementationOnce(() => b.promise));
  const load = kind === "background" ? loadBgTaskSnapshot : loadResearchSnapshot;
  const row = kind === "background" ? bg : research;
  const first = load("s"), second = load("s");
  a.resolve(Response.json([{ ...row, status: "completed" }]));
  await first;
  b.resolve(Response.json([{ ...row }]));
  await second;
  expect((kind === "background" ? useSessionStore.getState().bgTasks.s : useSessionStore.getState().research.s)[0].status).toBe("completed");
});

it.each(["background", "research"] as const)("accepts an older-started completed %s response after the newer running response", async (kind) => {
  const a = deferred(), b = deferred();
  vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => a.promise).mockImplementationOnce(() => b.promise));
  const load = kind === "background" ? loadBgTaskSnapshot : loadResearchSnapshot;
  const row = kind === "background" ? bg : research;
  const first = load("s"), second = load("s");
  b.resolve(Response.json([{ ...row }]));
  await second;
  a.resolve(Response.json([{ ...row, status: "completed" }]));
  await first;
  expect((kind === "background" ? useSessionStore.getState().bgTasks.s : useSessionStore.getState().research.s)[0].status).toBe("completed");
});

it("keeps real BG completion and a new task while both old lists finish", async () => {
  const a = deferred(), b = deferred();
  vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => a.promise).mockImplementationOnce(() => b.promise));
  const first = loadBgTaskSnapshot("s"), second = loadBgTaskSnapshot("s");
  handleWsMessage({ type: "bg_completed", session_id: "s", task_id: "task", status: "completed", exit_code: 0 });
  handleWsMessage({ type: "bg_started", session_id: "s", task_id: "new", command: "pwd" });
  b.resolve(Response.json([bg])); await second;
  a.resolve(Response.json([bg])); await first;
  expect(useSessionStore.getState().bgTasks.s).toEqual([
    expect.objectContaining({ id: "task", status: "completed" }), expect.objectContaining({ id: "new", status: "running" }),
  ]);
});

it.each(["unknown", null] as const)("allows an old list's terminal fact without replacing newer detail delivery (%s)", async (disposition) => {
  const a = deferred(), b = deferred();
  vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => a.promise).mockImplementationOnce(() => b.promise));
  const list = loadBgTaskSnapshot("s");
  const detail = observeSessionAuxiliary("s", "background", "task");
  b.resolve(Response.json({ ...bg, stdout: "Complete fetched output", delivery_disposition: disposition,
    delivery_error: disposition ? "New delivery" : null }));
  await detail.done;
  a.resolve(Response.json([{ ...bg, status: "completed", delivery_disposition: "stopped", delivery_error: "Old delivery" }]));
  await list;
  expect(useSessionStore.getState().bgTasks.s[0]).toMatchObject({
    status: "completed", stdout: "Complete fetched output",
    delivery_disposition: disposition, delivery_error: disposition ? "New delivery" : null,
  });
});

it.each(["background", "research"] as const)("cancels only the %s observation", async (kind) => {
  const response = deferred();
  const fetcher = vi.fn((_url: RequestInfo | URL) => response.promise);
  vi.stubGlobal("fetch", fetcher);
  const read = observeSessionAuxiliary("s", kind);
  read.cancel();
  response.resolve(Response.json([{ ...(kind === "background" ? bg : research), status: "completed" }]));
  await read.done;
  expect((kind === "background" ? useSessionStore.getState().bgTasks.s : useSessionStore.getState().research.s)[0].status).toBe("running");
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(String(fetcher.mock.calls[0][0])).not.toContain("cancel");
});

it("keeps receipt and parent processing facts distinct across a late research list", async () => {
  const response = deferred();
  vi.stubGlobal("fetch", vi.fn(() => response.promise));
  const read = loadResearchSnapshot("s");
  handleWsMessage({ type: "research_completed", session_id: "s", research_id: "task" });
  handleWsMessage({ type: "research_delivery", session_id: "s", research_id: "task", injection_status: "delivered",
    delivery_receipt: { request_id: "return", version: 2, stage: "queued", browser_confirmed_version: 1 } });
  response.resolve(Response.json([{ ...research, status: "completed", injection_status: "pending" }]));
  await read;
  expect(useSessionStore.getState().research.s[0]).toMatchObject({ injection_status: "delivered",
    delivery_receipt: { stage: "queued", version: 2, browser_confirmed_version: 1 } });
});
