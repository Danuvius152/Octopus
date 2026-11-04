import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import { controlWikiJob, loadWikiJobDetail, reconcileWikiJobs, refreshWikiJobs } from "./wikiJobs";
import { wikiJob } from "./wikiJobs.fixture";
import { useSessionStore } from "../stores/sessionStore";
import { handleWsMessage } from "../hooks/useWebSocket";

beforeEach(() => useSessionStore.setState({
  token: "token-a", wikiJobs: {}, wikiJobDirty: {}, wikiJobReadErrors: {}, activeSessionId: "original", dispatchGeneration: "service-a",
  dispatchStates: {}, retiredDispatchGenerations: [], messages: {}, lastAppliedSeq: { original: 9 },
}));
afterEach(() => vi.unstubAllGlobals());

it("does not classify an old service's 404 as a missing task in the new service", async () => {
  let finish!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { finish = resolve; })));
  const pending = loadWikiJobDetail(wikiJob(), 1);
  useSessionStore.setState({ dispatchGeneration: "service-b" });
  finish(Response.json({ detail: "Wiki job not found" }, { status: 404 }));
  await expect(pending).rejects.toThrow("Account or service changed");
});

it("never replaces a newer completed version with a delayed running snapshot", () => {
  useSessionStore.getState().mergeWikiJobs("original", [wikiJob({ state_version: 5, status: "completed" })]);
  useSessionStore.getState().mergeWikiJobs("original", [wikiJob({ state_version: 4, status: "running" })]);
  expect(useSessionStore.getState().wikiJobs.original[0].status).toBe("completed");
  expect(useSessionStore.getState().wikiJobs.original).toHaveLength(1);
});

it("updates current availability independently of version while retaining the newest saved facts", () => {
  const store = useSessionStore.getState();
  store.mergeWikiJobs("original", [wikiJob({ state_version: 5, current_state: "available" })]);
  store.mergeWikiJobs("original", [wikiJob({ state_version: 3, status: "queued", current_state: "unavailable" })]);
  expect(useSessionStore.getState().wikiJobs.original[0])
    .toMatchObject({ state_version: 5, status: "running", current_state: "unavailable" });
  store.mergeWikiJobs("original", [wikiJob({ state_version: 5, current_state: "available" })]);
  expect(useSessionStore.getState().wikiJobs.original[0].current_state).toBe("available");
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
  handleWsMessage({ type: "wiki_job_changed", session_id: "original", vault_id: "vault-1", job_id: "job-1", state_version: 1 });
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
  await refreshWikiJobs("original");
  const post = fetcher.mock.calls.find(([url]) => String(url).includes("/cancel"));
  expect(JSON.parse((post as unknown as [string, RequestInit])[1].body as string)).toEqual({ expected_attempt: 1, expected_vault_id: "vault-1" });
  expect(fetcher.mock.calls.filter(([url]) => String(url).includes("/cancel"))).toHaveLength(1);
  expect(useSessionStore.getState().wikiJobs.original[0].attempt).toBe(2);
});

it("clears another account's cached cards and rejects malformed card data", async () => {
  useSessionStore.getState().mergeWikiJobs("original", [wikiJob()]);
  useSessionStore.getState().setToken("token-b");
  expect(useSessionStore.getState().wikiJobs).toEqual({});
  expect(useSessionStore.getState().wikiJobDirty).toEqual({});
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify([{ job_id: "broken" }]))));
  await expect(refreshWikiJobs("original")).rejects.toThrow("invalid state snapshot");
  expect(useSessionStore.getState().wikiJobs).toEqual({});
});

it("recovers the exact old-vault card after a failed terminal refresh and session switch", async () => {
  useSessionStore.getState().mergeWikiJobs("original", [wikiJob()]);
  const fetcher = vi.fn().mockResolvedValueOnce(new Response("unavailable", { status: 503 }))
    .mockImplementation(async (url: string) => new Response(JSON.stringify(
      String(url).includes("job_id=job-1") && String(url).includes("vault_id=vault-1")
        ? [wikiJob({ status: "completed", state_version: 3 })] : [],
    )));
  vi.stubGlobal("fetch", fetcher);
  handleWsMessage({ type: "wiki_job_changed", session_id: "original", vault_id: "vault-1", job_id: "job-1", state_version: 3 });
  await expect(refreshWikiJobs("original")).rejects.toThrow("503");
  expect(useSessionStore.getState().wikiJobs.original[0].status).toBe("running");
  useSessionStore.setState({ activeSessionId: "another" });
  useSessionStore.setState({ activeSessionId: "original" });
  await refreshWikiJobs("original");
  expect(useSessionStore.getState().wikiJobs.original[0].status).toBe("completed");
  expect(fetcher.mock.calls.some(([url]) => String(url).includes("job_id=job-1") && String(url).includes("vault_id=vault-1"))).toBe(true);
});

it("retains an inactive session's invalidation until that session is opened", async () => {
  useSessionStore.setState({ activeSessionId: "another" });
  const fetcher = vi.fn(async (url: string) => Response.json(String(url).includes("job_id=job-1")
    ? [wikiJob({ status: "completed", state_version: 3 })] : []));
  vi.stubGlobal("fetch", fetcher);
  handleWsMessage({ type: "wiki_job_changed", session_id: "original", vault_id: "vault-1", job_id: "job-1", state_version: 3 });
  expect(fetcher).not.toHaveBeenCalled();
  useSessionStore.setState({ activeSessionId: "original" });
  await refreshWikiJobs("original");
  expect(useSessionStore.getState().wikiJobs.original[0].status).toBe("completed");
});

it("keeps a newer invalidation received while its exact GET is in flight", async () => {
  let finishExact!: (response: Response) => void;
  let requested = false;
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (!String(url).includes("job_id=")) return Promise.resolve(Response.json([]));
    if (!requested) {
      requested = true;
      return new Promise<Response>((resolve) => { finishExact = resolve; });
    }
    return Promise.resolve(Response.json([wikiJob({ status: "completed", state_version: 4 })]));
  }));
  handleWsMessage({ type: "wiki_job_changed", session_id: "original", vault_id: "vault-1", job_id: "job-1", state_version: 3 });
  await waitFor(() => expect(finishExact).toBeTypeOf("function"));
  handleWsMessage({ type: "wiki_job_changed", session_id: "original", vault_id: "vault-1", job_id: "job-1", state_version: 4 });
  const pending = refreshWikiJobs("original");
  finishExact(Response.json([wikiJob({ status: "running", state_version: 3 })]));
  await pending;
  expect(useSessionStore.getState().wikiJobs.original[0].state_version).toBe(4);
  expect(useSessionStore.getState().wikiJobs.original[0].status).toBe("completed");
});

it("recovers a failed control refresh using GET without repeating the POST", async () => {
  useSessionStore.getState().mergeWikiJobs("original", [wikiJob()]);
  let failed = false;
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") return Response.json({});
    if (!failed) { failed = true; return new Response("unavailable", { status: 503 }); }
    return Response.json(String(url).includes("job_id=job-1")
      ? [wikiJob({ status: "cancelled", state_version: 2, settlement_phase: "confirmed" })] : []);
  });
  vi.stubGlobal("fetch", fetcher);
  const result = await controlWikiJob("original", wikiJob(), "cancel");
  expect(result.state).toBe("accepted");
  if (result.state !== "accepted") throw new Error("Expected original POST receipt");
  expect(await result.refresh).toBe(false);
  expect(useSessionStore.getState().wikiJobReadErrors.original).toContain("503");
  expect(useSessionStore.getState().wikiJobs.original[0].status).toBe("running");
  await refreshWikiJobs("original");
  expect(useSessionStore.getState().wikiJobs.original[0].status).toBe("cancelled");
  expect(useSessionStore.getState().wikiJobReadErrors.original).toBeUndefined();
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it("refreshes a cached card beyond the first page after missing its terminal notice offline", async () => {
  const old = wikiJob({ vault_id: "historical-vault", job_id: "old-job" });
  useSessionStore.getState().mergeWikiJobs("original", [old]);
  useSessionStore.setState({ connected: false });
  const firstPage = Array.from({ length: 30 }, (_, index) => wikiJob({ job_id: `newer-${index}`, status: "completed" }));
  const fetcher = vi.fn(async (url: string) => Response.json(String(url).includes("job_id=old-job")
    && String(url).includes("vault_id=historical-vault") ? [{ ...old, status: "completed", state_version: 3 }] : firstPage));
  vi.stubGlobal("fetch", fetcher);
  useSessionStore.setState({ connected: true });
  await reconcileWikiJobs("original");
  expect(useSessionStore.getState().wikiJobs.original.find((card) => card.job_id === old.job_id)?.status).toBe("completed");
  expect(fetcher.mock.calls.some(([url]) => String(url).includes("job_id=old-job") && String(url).includes("vault_id=historical-vault"))).toBe(true);
});

it("rechecks completed cards beyond the first page for a retry owned by another entry", async () => {
  const old = wikiJob({ job_id: "old-completed", status: "completed", attempt: 1, state_version: 3 });
  useSessionStore.getState().mergeWikiJobs("original", [old]);
  const firstPage = Array.from({ length: 30 }, (_, index) => wikiJob({ job_id: `new-${index}`, status: "completed" }));
  const fetcher = vi.fn(async (url: string) => Response.json(String(url).includes("job_id=old-completed")
    ? [{ ...old, state_version: 6, attempt: 2, status: "running" }] : firstPage));
  vi.stubGlobal("fetch", fetcher);
  await reconcileWikiJobs("original");
  expect(useSessionStore.getState().wikiJobs.original.find((card) => card.job_id === old.job_id))
    .toMatchObject({ attempt: 2, state_version: 6, status: "running" });
});
