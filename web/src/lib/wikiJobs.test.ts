import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import { controlWikiJob, refreshWikiJobs } from "./wikiJobs";
import { wikiJob } from "./wikiJobs.fixture";
import { useSessionStore } from "../stores/sessionStore";
import { handleWsMessage } from "../hooks/useWebSocket";

beforeEach(() => useSessionStore.setState({
  token: "token-a", wikiJobs: {}, activeSessionId: "original", dispatchGeneration: "service-a",
  dispatchStates: {}, retiredDispatchGenerations: [], messages: {}, lastAppliedSeq: { original: 9 },
}));
afterEach(() => vi.unstubAllGlobals());

it("never replaces a newer completed version with a delayed running snapshot", () => {
  useSessionStore.getState().mergeWikiJobs("original", [wikiJob({ state_version: 5, status: "completed" })]);
  useSessionStore.getState().mergeWikiJobs("original", [wikiJob({ state_version: 4, status: "running" })]);
  expect(useSessionStore.getState().wikiJobs.original[0].status).toBe("completed");
  expect(useSessionStore.getState().wikiJobs.original).toHaveLength(1);
});

it.each(["account", "service", "initial-null-generation"])("rejects a late response after %s changes", async (change) => {
  if (change === "initial-null-generation") useSessionStore.setState({ dispatchGeneration: null });
  let finish!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { finish = resolve; })));
  const fetching = refreshWikiJobs("original");
  if (change === "account") useSessionStore.getState().setToken("another-account");
  else useSessionStore.setState({ dispatchGeneration: "service-b" });
  finish(new Response(JSON.stringify([wikiJob({ state_version: 99 })])));
  await fetching;
  expect(useSessionStore.getState().wikiJobs.original).toBeUndefined();
});

it("coalesces notifications but refetches an invalidation received during the request", async () => {
  let finish!: (response: Response) => void;
  const fetcher = vi.fn().mockImplementationOnce(() => new Promise<Response>((resolve) => { finish = resolve; }))
    .mockResolvedValueOnce(new Response(JSON.stringify([wikiJob({ state_version: 2, status: "completed" })])));
  vi.stubGlobal("fetch", fetcher);
  const first = refreshWikiJobs("original");
  const following = refreshWikiJobs("original");
  const last = refreshWikiJobs("original");
  finish(new Response(JSON.stringify([wikiJob()])));
  await Promise.all([first, following, last]);
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(useSessionStore.getState().wikiJobs.original[0].state_version).toBe(2);
});

it("refreshes from a notice without appending a model turn or moving the transcript watermark", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify([wikiJob()]))));
  handleWsMessage({ type: "wiki_job_changed", session_id: "original", job_id: "job-1", state_version: 1 });
  await waitFor(() => expect(useSessionStore.getState().wikiJobs.original).toHaveLength(1));
  expect(useSessionStore.getState().messages.original).toBeUndefined();
  expect(useSessionStore.getState().lastAppliedSeq.original).toBe(9);
});

it("sends the displayed attempt and vault once, then reports a stale control", async () => {
  const fetcher = vi.fn(async (url: string) => String(url).includes("/cancel")
    ? new Response("{}", { status: 409 })
    : new Response(JSON.stringify([wikiJob({ state_version: 3, attempt: 2 })])));
  vi.stubGlobal("fetch", fetcher);
  await expect(controlWikiJob("original", wikiJob(), "cancel")).rejects.toThrow("task changed");
  const post = fetcher.mock.calls.find(([url]) => String(url).includes("/cancel"));
  expect(JSON.parse((post as unknown as [string, RequestInit])[1].body as string)).toEqual({ expected_attempt: 1, expected_vault_id: "vault-1" });
  expect(fetcher.mock.calls.filter(([url]) => String(url).includes("/cancel"))).toHaveLength(1);
  expect(useSessionStore.getState().wikiJobs.original[0].attempt).toBe(2);
});

it("clears another account's cached cards and rejects malformed card data", async () => {
  useSessionStore.getState().mergeWikiJobs("original", [wikiJob()]);
  useSessionStore.getState().setToken("token-b");
  expect(useSessionStore.getState().wikiJobs).toEqual({});
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify([{ job_id: "broken" }]))));
  await expect(refreshWikiJobs("original")).rejects.toThrow("invalid state snapshot");
  expect(useSessionStore.getState().wikiJobs).toEqual({});
});
