import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { WikiJobCards } from "./WikiJobCards";
import { useSessionStore } from "../stores/sessionStore";
import { wikiJob } from "../lib/wikiJobs.fixture";

beforeEach(() => {
  useSessionStore.setState({ token: "token-a", wikiJobs: {}, dispatchGeneration: "service-a", connected: true });
  vi.stubGlobal("fetch", vi.fn(async () => new Response("[]")));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("shows persisted completed outputs after a chat is reopened", () => {
  useSessionStore.getState().mergeWikiJobs("original", [wikiJob({ status: "completed", state_version: 3,
    progress: { total: 2, completed: 2, attention: 0, failed: 0 }, outcome_count: 1,
    outcome_refs: [{ doc_id: "document", path: "Library/note.md", revision: "a".repeat(64) }],
  })]);
  render(<WikiJobCards sessionId="original" />);
  expect(screen.getByText(/Wiki maintenance · Completed/)).toBeTruthy();
  expect(screen.getByText(/2\/2 items completed/)).toBeTruthy();
  expect(screen.getByText(/Saved outputs \(1\)/)).toBeTruthy();
  expect(screen.queryByText("Retry unfinished work")).toBeNull();
});

it.each(["pending", "unconfirmed"] as const)("keeps retry closed while cancellation is %s", (phase) => {
  useSessionStore.getState().mergeWikiJobs("original", [wikiJob({ status: "cancelled", settlement_phase: phase })]);
  render(<WikiJobCards sessionId="original" />);
  expect(screen.getByRole("status")).toBeTruthy();
  expect(screen.queryByText("Retry unfinished work")).toBeNull();
});

it("distinguishes saved content awaiting indexing from completed work", () => {
  useSessionStore.getState().mergeWikiJobs("original", [wikiJob({ status: "completed_with_attention",
    effects: { recorded: 1, applied: 1, index_pending: 1 },
  })]);
  render(<WikiJobCards sessionId="original" />);
  expect(screen.getByText(/Content saved; indexing remains pending/)).toBeTruthy();
  expect(screen.getByText(/Needs review/)).toBeTruthy();
  expect(screen.getByText("Retry unfinished work")).toBeTruthy();
});

it("cancels the displayed task and refreshes its persistent result without a message POST", async () => {
  useSessionStore.getState().mergeWikiJobs("original", [wikiJob()]);
  const fetcher = vi.fn(async (_url: string, init?: RequestInit) => init?.method === "POST"
    ? new Response("{}") : new Response(JSON.stringify([wikiJob({ state_version: 2, status: "cancelled", settlement_phase: "confirmed" })])));
  vi.stubGlobal("fetch", fetcher);
  render(<WikiJobCards sessionId="original" />);
  fireEvent.click(screen.getByText("Cancel maintenance"));
  await waitFor(() => expect(screen.getByText("Retry unfinished work")).toBeTruthy());
  const post = fetcher.mock.calls.filter(([, init]) => init?.method === "POST");
  expect(post).toHaveLength(1);
  expect(post[0][0]).toContain("/api/wiki/jobs/job-1/cancel");
  expect(JSON.parse(post[0][1]!.body as string)).toEqual({ expected_attempt: 1, expected_vault_id: "vault-1" });
});
