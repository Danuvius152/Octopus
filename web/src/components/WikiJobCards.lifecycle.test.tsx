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

it("labels an unreadable current task as unavailable while preserving its last saved summary", async () => {
  const saved = wikiJob({ current_state: "available" });
  vi.stubGlobal("fetch", vi.fn(async () => Response.json([saved])));
  useSessionStore.getState().mergeWikiJobs("original", [saved]);
  render(<WikiJobCards sessionId="original" />);
  await act(async () => {});
  await act(async () => useSessionStore.getState().mergeWikiJobs("original", [{ ...saved, current_state: "unavailable" }]));
  expect(screen.getByText("Wiki maintenance · Current state unavailable")).toBeTruthy();
  expect(screen.getByText(/Saved state: Reviewing/)).toBeTruthy();
  expect(screen.getByText(saved.scope)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Cancel maintenance" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Task details" })).toBeNull();
});

it("opens details and controls with the new attempt discovered beyond the first page", async () => {
  const old = wikiJob({ job_id: "old-job", status: "completed", state_version: 3 });
  const current = { ...old, status: "running", attempt: 2, state_version: 6 };
  const page = Array.from({ length: 30 }, (_, index) => wikiJob({ job_id: `newer-${index}`, status: "completed" }));
  const posts: RequestInit[] = [];
  const details: URL[] = [];
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") { posts.push(init); return Response.json({}); }
    if (url.includes("/details")) {
      details.push(new URL(url));
      return Response.json({ job_id: current.job_id, vault_id: current.vault_id, state_version: 6,
        attempt: 2, current_attempt: 2, attempts: [1, 2], status: "running", settlement_phase: "none",
        error: null, items: [], outputs: [], next_offset: null,
        actions: { verify: false, retry: false, readmit: false, blocked_reason: null } });
    }
    return Response.json(new URL(url).searchParams.get("job_id") === old.job_id ? [current] : page);
  });
  vi.stubGlobal("fetch", fetcher);
  useSessionStore.getState().mergeWikiJobs("original", [old]);
  render(<WikiJobCards sessionId="original" />);
  await screen.findByText("Wiki maintenance · Reviewing");
  fireEvent.click(screen.getByRole("button", { name: "Task details" }));
  await screen.findByLabelText("Maintenance attempt");
  expect(details[0].searchParams.get("attempt")).toBe("2");
  fireEvent.click(screen.getByRole("button", { name: "Cancel maintenance" }));
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(JSON.parse(String(posts[0].body))).toMatchObject({ expected_attempt: 2, expected_vault_id: current.vault_id });
});

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

it("retires a stalled page on reconnect and lets the new observation page again", async () => {
  const first = Array.from({ length: 30 }, (_, index) => wikiJob({ job_id: `job-${index}`, status: "completed" }));
  let releaseOld!: (response: Response) => void;
  let releaseNew!: (response: Response) => void;
  let pages = 0;
  let firstPages = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (new URL(url).searchParams.get("offset") !== "30") {
      firstPages++;
      return Response.json(first);
    }
    pages++;
    return new Promise<Response>((resolve) => {
      if (pages === 1) releaseOld = resolve; else releaseNew = resolve;
    });
  }));
  render(<WikiJobCards sessionId="original" />);
  const more = await screen.findByRole("button", { name: "More maintenance tasks" });
  fireEvent.click(more);
  await waitFor(() => expect(pages).toBe(1));
  expect((more as HTMLButtonElement).disabled).toBe(true);

  await act(async () => useSessionStore.setState({ connected: false }));
  await act(async () => useSessionStore.setState({ connected: true }));
  await waitFor(() => expect(firstPages).toBeGreaterThanOrEqual(3));
  const resumedMore = await screen.findByRole("button", { name: "More maintenance tasks" });
  await waitFor(() => expect((resumedMore as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(resumedMore);
  await waitFor(() => expect(pages).toBe(2));

  await act(async () => releaseOld(Response.json([wikiJob({ job_id: "late-page", status: "completed" })])));
  expect((resumedMore as HTMLButtonElement).disabled).toBe(true);
  expect(useSessionStore.getState().wikiJobs.original.some((card) => card.job_id === "late-page")).toBe(false);
  await act(async () => releaseNew(Response.json([wikiJob({ job_id: "older", status: "completed" })])));
  await screen.findByRole("button", { name: "Completed maintenance (31 loaded)" });
  expect(screen.queryByRole("button", { name: "More maintenance tasks" })).toBeNull();
});
