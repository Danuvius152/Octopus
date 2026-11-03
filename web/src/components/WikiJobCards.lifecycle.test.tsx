import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { WikiJobCards } from "./WikiJobCards";
import { useSessionStore } from "../stores/sessionStore";
import { wikiJob } from "../lib/wikiJobs.fixture";

beforeEach(() => {
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useSessionStore.setState({ token: "token-a", dispatchGeneration: "service-a", connected: true });
  vi.stubGlobal("fetch", vi.fn(async () => Response.json([])));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("removes settled success from the activity area and keeps its saved outputs in history", async () => {
  useSessionStore.getState().mergeWikiJobs("original", [wikiJob()]);
  render(<WikiJobCards sessionId="original" />);
  expect(screen.getByText(/Wiki maintenance · Reviewing/)).toBeTruthy();
  await act(async () => useSessionStore.getState().mergeWikiJobs("original", [wikiJob({
    state_version: 2, status: "completed", settlement_phase: "confirmed",
    progress: { total: 2, completed: 2, attention: 0, failed: 0 }, outcome_count: 1,
    outcome_refs: [{ doc_id: "document", path: "Library/note.md", revision: "a".repeat(64) }],
  })]));
  expect(screen.queryByText(/Wiki maintenance ·/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Completed maintenance (1 loaded)" }));
  expect(screen.getByText(/Wiki maintenance · Completed/)).toBeTruthy();
  expect(screen.getByText(/Library\/note.md/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Completed maintenance (1 loaded)" }));
  expect(screen.queryByText(/Wiki maintenance ·/)).toBeNull();
  expect(useSessionStore.getState().wikiJobs.original[0].outcome_count).toBe(1);
});

it.each([
  { settlement_phase: "pending" as const },
  { settlement_phase: "attention" as const },
  { settlement_phase: "unconfirmed" as const },
  { recording_pending: 1 },
  { effects: { recorded: 1, applied: 1, index_pending: 1 } },
])("keeps success visible until its remaining work settles: %j", async (pending) => {
  useSessionStore.getState().mergeWikiJobs("original", [wikiJob({ status: "completed", ...pending })]);
  render(<WikiJobCards sessionId="original" />);
  expect(screen.getByText(/Wiki maintenance · Completed/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /Completed maintenance/ })).toBeNull();
  await act(async () => useSessionStore.getState().mergeWikiJobs("original", [wikiJob({
    state_version: 2, status: "completed", settlement_phase: "confirmed",
  })]));
  expect(screen.queryByText(/Wiki maintenance · Completed/)).toBeNull();
  expect(screen.getByRole("button", { name: "Completed maintenance (1 loaded)" })).toBeTruthy();
});

it.each(["queued", "running", "interrupted", "failed", "cancelled", "completed_with_attention", "attention", "unknown"])(
  "keeps %s tasks visible and brings a newer attempt back out of history", async (status) => {
    useSessionStore.getState().mergeWikiJobs("original", [wikiJob({ status: "completed" })]);
    render(<WikiJobCards sessionId="original" />);
    expect(screen.queryByText(/Wiki maintenance ·/)).toBeNull();
    await act(async () => useSessionStore.getState().mergeWikiJobs("original", [wikiJob({ state_version: 2, attempt: 2, status })]));
    expect(screen.getByText(/Wiki maintenance ·/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Completed maintenance/ })).toBeNull();
  },
);

it("restores collapsed history from HTTP on reopen and keeps older pages reachable", async () => {
  const first = Array.from({ length: 30 }, (_, index) => wikiJob({ job_id: `job-${index}`, status: "completed" }));
  vi.stubGlobal("fetch", vi.fn(async (url: string) => Response.json(
    new URL(url).searchParams.get("offset") === "30" ? [wikiJob({ job_id: "older", status: "completed" })] : first,
  )));
  render(<WikiJobCards sessionId="original" />);
  const history = await screen.findByRole("button", { name: "Completed maintenance (30 loaded)" });
  expect(screen.queryByText(/Wiki maintenance ·/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "More maintenance tasks" }));
  await waitFor(() => expect(history.textContent).toBe("Completed maintenance (31 loaded)"));
  fireEvent.click(history);
  expect(screen.getAllByText(/Wiki maintenance · Completed/)).toHaveLength(31);
  expect(screen.queryByRole("button", { name: "More maintenance tasks" })).toBeNull();
});
