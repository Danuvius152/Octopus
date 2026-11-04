import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { loadWikiJobDetail, loadWikiJobOutput, refreshWikiJobs, retireWikiJobs } from "./wikiJobs";
import { wikiJob } from "./wikiJobs.fixture";
import { useSessionStore } from "../stores/sessionStore";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => { resolve = yes; });
  return { promise, resolve };
}

beforeEach(() => {
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useSessionStore.setState({ token: "token-a", activeSessionId: "original", dispatchGeneration: "service-a", connected: true });
  vi.useFakeTimers();
});
afterEach(() => { useSessionStore.setState({ token: "" }); vi.useRealTimers(); vi.unstubAllGlobals(); });

it.each(["headers", "body"])("bounds a stalled %s read and releases its coalescing slot", async (phase) => {
  const old = deferred<Response>();
  const body = deferred<unknown>();
  const fetcher = vi.fn().mockImplementationOnce(() => phase === "headers" ? old.promise
    : Promise.resolve({ ok: true, json: () => body.promise } as Response))
    .mockResolvedValue(Response.json([wikiJob({ state_version: 2 })]));
  vi.stubGlobal("fetch", fetcher);
  const pending = refreshWikiJobs("original");
  let settled = false;
  const observed = pending.then(() => { settled = true; }, () => { settled = true; });
  await vi.advanceTimersByTimeAsync(30_001);
  expect(settled).toBe(true);
  await observed;
  await refreshWikiJobs("original");
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(useSessionStore.getState().wikiJobs.original[0].state_version).toBe(2);
  old.resolve(Response.json([wikiJob({ state_version: 99 })]));
  body.resolve([wikiJob({ state_version: 99 })]);
  await vi.advanceTimersByTimeAsync(0);
  expect(useSessionStore.getState().wikiJobs.original[0].state_version).toBe(2);
});

it.each(["headers", "body"])("reconnect retires old %s and its late finally cannot release a replacement slot", async (phase) => {
  const old = deferred<Response>();
  const body = deferred<unknown>();
  const next = deferred<Response>();
  const fetcher = vi.fn().mockImplementationOnce(() => phase === "headers" ? old.promise
    : Promise.resolve({ ok: true, json: () => body.promise } as Response))
    .mockImplementationOnce(() => next.promise).mockResolvedValue(Response.json([wikiJob({ state_version: 2 })]));
  vi.stubGlobal("fetch", fetcher);
  useSessionStore.getState().mergeWikiJobs("original", [wikiJob()]);
  const first = refreshWikiJobs("original");
  await vi.advanceTimersByTimeAsync(0);
  useSessionStore.setState({ connected: false });
  useSessionStore.setState({ connected: true });
  const replacement = refreshWikiJobs("original");
  expect(fetcher).toHaveBeenCalledTimes(2);
  old.resolve(Response.json([wikiJob({ state_version: 99 })]));
  body.resolve([wikiJob({ state_version: 99 })]);
  await first;
  await vi.advanceTimersByTimeAsync(0);
  expect(useSessionStore.getState().wikiJobs.original[0].state_version).toBe(1);
  expect(refreshWikiJobs("original")).toBe(replacement);
  expect(fetcher).toHaveBeenCalledTimes(2);
  next.resolve(Response.json([wikiJob({ state_version: 2 })]));
  await replacement;
  expect(useSessionStore.getState().wikiJobs.original[0].state_version).toBe(2);
});

it.each(["account", "account-ABA", "service", "session", "view", "end"])("retiring %s prevents a late body from entering a new observation", async (change) => {
  const old = deferred<unknown>();
  const fetcher = vi.fn().mockResolvedValueOnce({ ok: true, json: () => old.promise } as Response)
    .mockResolvedValue(Response.json([wikiJob({ state_version: 2 })]));
  vi.stubGlobal("fetch", fetcher);
  const first = refreshWikiJobs("original");
  await vi.advanceTimersByTimeAsync(0);
  if (change.startsWith("account")) {
    useSessionStore.getState().setToken("token-b");
    if (change === "account-ABA") useSessionStore.getState().setToken("token-a");
    useSessionStore.setState({ activeSessionId: "original", dispatchGeneration: "service-a", connected: true });
  } else if (change === "service") useSessionStore.setState({ dispatchGeneration: "service-b" });
  else if (change === "session") useSessionStore.setState({ activeSessionId: "another" });
  else if (change === "view") useSessionStore.setState({ mainView: "schedules" });
  else retireWikiJobs("original", first);
  await refreshWikiJobs("original");
  old.resolve([wikiJob({ state_version: 99 })]);
  await first;
  expect(useSessionStore.getState().wikiJobs.original[0].state_version).toBe(2);
});

for (const operation of ["details", "evidence-page", "output", "cards-page"] as const) {
  it.each(["headers", "body"])(`bounds ${operation} stalled at %s`, async (phase) => {
    const stalled = deferred<Response>();
    const body = deferred<unknown>();
    vi.stubGlobal("fetch", vi.fn(() => phase === "headers" ? stalled.promise
      : Promise.resolve({ ok: true, json: () => body.promise } as Response)));
    const pending = operation === "details" ? loadWikiJobDetail(wikiJob(), 1)
      : operation === "evidence-page" ? loadWikiJobDetail(wikiJob(), 1, 30)
      : operation === "output" ? loadWikiJobOutput(wikiJob(), 1, "original-receipt", "history")
      : refreshWikiJobs("original", 30);
    const failure = pending.catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(30_001);
    expect(await failure).toBeInstanceOf(Error);
    expect(String(await failure)).toContain("timed out after 30 seconds");
  });
}
