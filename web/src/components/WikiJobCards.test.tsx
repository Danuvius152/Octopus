import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { WikiJobCards } from "./WikiJobCards";
import { useSessionStore } from "../stores/sessionStore";
import { wikiJob } from "../lib/wikiJobs.fixture";

beforeEach(() => {
  useSessionStore.setState({ token: "token-a", wikiJobs: {}, wikiJobDirty: {}, wikiJobReadErrors: {}, dispatchGeneration: "service-a", connected: true });
  vi.stubGlobal("fetch", vi.fn(async () => new Response("[]")));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("keeps an accepted cancellation and saved data when confirmation fails, and only retries GET", async () => {
  const card = wikiJob();
  let controlled = false;
  let recovered = false;
  const posts: RequestInit[] = [];
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "POST") { controlled = true; posts.push(init); return Response.json({}); }
    if (controlled && !recovered) return Response.json({ detail: "confirmation offline" }, { status: 503 });
    return Response.json([recovered ? { ...card, state_version: 2, status: "cancelled" } : card]);
  }));
  useSessionStore.getState().mergeWikiJobs("original", [card]);
  render(<WikiJobCards sessionId="original" />);
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Cancel maintenance" }));
  await screen.findByText("Cancellation request accepted for attempt 1.");
  expect(await screen.findByText(/State refresh failed:.*503/)).toBeTruthy();
  expect(screen.getByText(card.scope)).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
  recovered = true;
  fireEvent.click(screen.getByRole("button", { name: "Refresh task state" }));
  await screen.findByText("Wiki maintenance · Cancelled");
  expect(screen.getByText("Cancellation request accepted for attempt 1.")).toBeTruthy();
  expect(screen.queryByText(/State refresh failed/)).toBeNull();
  expect(posts).toHaveLength(1);
});

it.each([403, 409])("retains the original HTTP %s control error when GET also fails and later recovers", async (status) => {
  const card = wikiJob();
  let controlled = false;
  let recovered = false;
  let posts = 0;
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "POST") { controlled = true; posts++; return Response.json({}, { status }); }
    if (controlled && !recovered) return Response.json({}, { status: 503 });
    return Response.json([card]);
  }));
  useSessionStore.getState().mergeWikiJobs("original", [card]);
  render(<WikiJobCards sessionId="original" />);
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Cancel maintenance" }));
  const error = await screen.findByRole("alert");
  const original = error.textContent;
  expect(original).toContain(status === 409 ? "task changed" : "HTTP 403");
  await screen.findByText(/State refresh failed:.*503/);
  recovered = true;
  fireEvent.click(screen.getByRole("button", { name: "Refresh task state" }));
  await waitFor(() => expect(screen.queryByText(/State refresh failed/)).toBeNull());
  expect(error.textContent).toBe(original);
  expect(posts).toBe(1);
});

it("preserves accepted verification and existing detail evidence when its card refresh fails", async () => {
  const card = wikiJob({ status: "completed_with_attention" });
  let controlled = false;
  let posts = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") { controlled = true; posts++; return Response.json({}); }
    if (url.includes("/details")) return Response.json({ job_id: card.job_id, vault_id: card.vault_id,
      state_version: 1, attempt: 1, current_attempt: 1, attempts: [1], status: card.status,
      settlement_phase: "none", error: "Original retained evidence", items: [], outputs: [], next_offset: null,
      actions: { verify: true, retry: false, readmit: false, blocked_reason: null } });
    return controlled ? Response.json({}, { status: 503 }) : Response.json([card]);
  }));
  useSessionStore.getState().mergeWikiJobs("original", [card]);
  render(<WikiJobCards sessionId="original" />);
  fireEvent.click(screen.getByRole("button", { name: "Task details" }));
  const verify = await screen.findByRole("button", { name: "Verify saved result and index" });
  await waitFor(() => expect(verify).toBeEnabled());
  fireEvent.click(verify);
  await screen.findByText("Verification request accepted for attempt 1.");
  await screen.findByText(/State refresh failed:.*503/);
  expect(screen.getByText("Original retained evidence")).toBeTruthy();
  expect(posts).toBe(1);
});

it("shows persisted completed outputs after a chat is reopened", () => {
  useSessionStore.getState().mergeWikiJobs("original", [wikiJob({ status: "completed", state_version: 3,
    progress: { total: 2, completed: 2, attention: 0, failed: 0 }, outcome_count: 1,
    outcome_refs: [{ doc_id: "document", path: "Library/note.md", revision: "a".repeat(64) }],
  })]);
  render(<WikiJobCards sessionId="original" />);
  fireEvent.click(screen.getByRole("button", { name: "Completed maintenance (1 loaded)" }));
  expect(screen.getByText(/Wiki maintenance · Completed/)).toBeTruthy();
  expect(screen.getByText(/2\/2 items completed/)).toBeTruthy();
  expect(screen.getByText(/Saved outputs \(1\)/)).toBeTruthy();
  expect(screen.queryByText("Retry unfinished work")).toBeNull();
});

it("keeps the original card read-only when task details are no longer available", async () => {
  const card = wikiJob({ status: "completed", outcome_count: 12, outcome_refs_truncated: true,
    outcome_refs: [{ doc_id: "document", path: "Library/note.md", revision: "a".repeat(64) }],
  });
  useSessionStore.getState().mergeWikiJobs("original", [card]);
  const fetcher = vi.fn(async (url: string, _init?: RequestInit) => String(url).includes("/details")
    ? Response.json({ detail: "Wiki job not found" }, { status: 404 }) : Response.json([card]));
  vi.stubGlobal("fetch", fetcher);
  render(<WikiJobCards sessionId="original" />);
  fireEvent.click(screen.getByRole("button", { name: "Completed maintenance (1 loaded)" }));
  fireEvent.click(screen.getByRole("button", { name: "Task details" }));
  await screen.findByText("Task details are unavailable. The saved summary remains here.");
  expect(screen.getByText(/Wiki maintenance · Completed/)).toBeTruthy();
  expect(screen.getByText(/Library\/note.md/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Task details" })).toBeNull();
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.queryByText("Additional outputs are retained in the Wiki task details.")).toBeNull();
  expect(screen.queryByText("Verify saved result and index")).toBeNull();
  expect(screen.queryByText("Continue unfinished work")).toBeNull();
  expect(fetcher.mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
});

it.each([403, 503])("does not treat HTTP %s as missing task details", async (status) => {
  const card = wikiJob({ status: "completed" });
  useSessionStore.getState().mergeWikiJobs("original", [card]);
  vi.stubGlobal("fetch", vi.fn(async (url: string) => String(url).includes("/details")
    ? Response.json({ detail: "Cannot read this task" }, { status }) : Response.json([card])));
  render(<WikiJobCards sessionId="original" />);
  fireEvent.click(screen.getByRole("button", { name: "Completed maintenance (1 loaded)" }));
  fireEvent.click(screen.getByRole("button", { name: "Task details" }));
  expect((await screen.findByRole("alert")).textContent).toBe("Cannot read this task");
  expect(screen.queryByText("Task details are unavailable. The saved summary remains here.")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Close details" }));
  expect(screen.getByRole("button", { name: "Task details" })).toBeTruthy();
});

it.each(["pending", "attention", "unconfirmed"] as const)("keeps retry closed while cancellation is %s", (phase) => {
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
  expect(screen.getByText("Task details")).toBeTruthy();
});

it("cancels the displayed task and refreshes its persistent result without a message POST", async () => {
  useSessionStore.getState().mergeWikiJobs("original", [wikiJob()]);
  const fetcher = vi.fn(async (_url: string, init?: RequestInit) => init?.method === "POST"
    ? new Response("{}") : new Response(JSON.stringify([wikiJob({ state_version: 2, status: "cancelled", settlement_phase: "confirmed" })])));
  vi.stubGlobal("fetch", fetcher);
  render(<WikiJobCards sessionId="original" />);
  fireEvent.click(screen.getByText("Cancel maintenance"));
  await waitFor(() => expect(screen.getByText(/Wiki maintenance · Cancelled/)).toBeTruthy());
  const post = fetcher.mock.calls.filter(([, init]) => init?.method === "POST");
  expect(post).toHaveLength(1);
  expect(post[0][0]).toContain("/api/wiki/jobs/job-1/cancel");
  expect(JSON.parse(post[0][1]!.body as string)).toEqual({ expected_attempt: 1, expected_vault_id: "vault-1" });
});

it("reconciles a retained task missed while disconnected even after it leaves the first page", async () => {
  useSessionStore.setState({ connected: false });
  const old = wikiJob({ vault_id: "historical-vault", job_id: "old-job" });
  useSessionStore.getState().mergeWikiJobs("original", [old]);
  const firstPage = Array.from({ length: 30 }, (_, index) => wikiJob({ job_id: `newer-${index}`, status: "completed" }));
  const fetcher = vi.fn().mockResolvedValueOnce(new Response("offline", { status: 503 }))
    .mockImplementation(async (url: string) => Response.json(String(url).includes("job_id=old-job")
      && String(url).includes("vault_id=historical-vault") ? [{ ...old, status: "completed", state_version: 3 }] : firstPage));
  vi.stubGlobal("fetch", fetcher);
  const { container } = render(<WikiJobCards sessionId="original" />);
  await screen.findByText(/State refresh failed:.*503/);
  await act(async () => useSessionStore.setState({ connected: true }));
  await screen.findByRole("button", { name: "Completed maintenance (31 loaded)" });
  expect(container.querySelector('[data-wiki-job="old-job"]')).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Completed maintenance (31 loaded)" }));
  await waitFor(() => expect(container.querySelector('[data-wiki-job="old-job"]')).toHaveAttribute("data-status", "completed"));
  expect(fetcher.mock.calls.every(([, init]) => !(init as RequestInit | undefined)?.method || (init as RequestInit).method === "GET")).toBe(true);
});

it("opens complete historical evidence and moved current text using only GET requests", async () => {
  useSessionStore.getState().mergeWikiJobs("original", [wikiJob({ status: "completed" })]);
  const diagnostic = "Complete error " + "retained condition ".repeat(100);
  const fetcher = vi.fn(async (url: string, _init?: RequestInit) => {
    if (String(url).includes("/details")) return Response.json({
      job_id: "job-1", vault_id: "vault-1", state_version: 3, attempt: 1, current_attempt: 2, attempts: [1, 2],
      status: "completed", settlement_phase: "none", error: diagnostic, next_offset: null,
      items: [{ key: "doc", doc: "doc", path: "Library/original.md", status: "done", error: null, execution: null }],
      outputs: [{ receipt_id: "receipt", doc: "doc", path: "Library/original.md", current_path: "Library/moved.md",
        revision: "a".repeat(64), current_revision: "b".repeat(64), operation: "no_change", status: "applied",
        reason: "All original conditions are retained.", change_id: null, attempt: 1, details: {} }],
      actions: { verify: false, retry: false, readmit: false, blocked_reason: null },
    });
    if (String(url).includes("/outputs/")) return Response.json({
      receipt_id: "receipt", doc: "doc", path: "Library/original.md", revision: "a".repeat(64),
      view: "history", content: "Original historical body", start: 0, next_start: null, characters: 24,
    });
    return Response.json([wikiJob({ status: "completed" })]);
  });
  vi.stubGlobal("fetch", fetcher);
  render(<WikiJobCards sessionId="original" />);
  fireEvent.click(screen.getByRole("button", { name: "Completed maintenance (1 loaded)" }));
  fireEvent.click(screen.getByText("Task details"));
  await waitFor(() => expect(screen.getByText(diagnostic.trim()).textContent).toBe(diagnostic));
  expect(screen.getByText(/Library\/moved.md/)).toBeTruthy();
  expect(screen.getByText("All original conditions are retained.")).toBeTruthy();
  fireEvent.click(screen.getByText("Historical output"));
  await waitFor(() => expect(screen.getByText("Original historical body")).toBeTruthy());
  expect(fetcher.mock.calls.every(([, init]) => !(init as RequestInit | undefined)?.method || (init as RequestInit).method === "GET")).toBe(true);
});

it("reuses the explicit continuation identity after a lost control response", async () => {
  const card = wikiJob({ status: "cancelled", settlement_phase: "confirmed" });
  useSessionStore.getState().mergeWikiJobs("original", [card]);
  const posts: { url: string; body: Record<string, unknown> }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      posts.push({ url: String(url), body: JSON.parse(String(init.body)) });
      return posts.length === 1 ? new Response("unavailable", { status: 503 }) : Response.json({});
    }
    if (String(url).includes("/details")) return Response.json({
      job_id: card.job_id, vault_id: card.vault_id, state_version: 3, attempt: 1, current_attempt: 1, attempts: [1],
      status: "cancelled", settlement_phase: "confirmed", error: null, items: [], outputs: [], next_offset: null,
      actions: { verify: true, retry: true, readmit: false, blocked_reason: null },
    });
    return Response.json([card]);
  }));
  render(<WikiJobCards sessionId="original" />);
  fireEvent.click(screen.getByText("Task details"));
  await waitFor(() => expect(screen.getByText("Continue unfinished work")).toBeTruthy());
  fireEvent.click(screen.getByText("Continue unfinished work"));
  await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("503"));
  fireEvent.click(screen.getByText("Continue unfinished work"));
  await waitFor(() => expect(posts).toHaveLength(2));
  expect(posts[0].body).toEqual(posts[1].body);
  expect(posts[0].body.operation_id).toEqual(expect.any(String));
  expect(posts.every((post) => post.url.endsWith("/retry"))).toBe(true);
});

it("starts a new explicit verification after a confirmed verification and content repair", async () => {
  const card = wikiJob({ status: "completed_with_attention" });
  useSessionStore.getState().mergeWikiJobs("original", [card]);
  const accepted = new Set<string>();
  const operations: string[] = [];
  let repaired = false;
  let verified = false;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const operation = JSON.parse(String(init.body)).operation_id;
      operations.push(operation);
      if (!accepted.has(operation)) { accepted.add(operation); verified = repaired; }
      return Response.json({});
    }
    if (String(url).includes("/details")) return Response.json({
      job_id: card.job_id, vault_id: card.vault_id, state_version: 3, attempt: 1, current_attempt: 1, attempts: [1],
      status: verified ? "completed" : "completed_with_attention", settlement_phase: "none",
      error: verified ? null : "Saved output still needs review", items: [], outputs: [], next_offset: null,
      actions: { verify: true, retry: false, readmit: false, blocked_reason: null },
    });
    return Response.json([card]);
  }));
  render(<WikiJobCards sessionId="original" />);
  fireEvent.click(screen.getByText("Task details"));
  const verify = await screen.findByText("Verify saved result and index");
  fireEvent.click(verify);
  await waitFor(() => expect(operations).toHaveLength(1));
  await screen.findByText("Saved output still needs review");
  repaired = true;
  fireEvent.click(await screen.findByText("Verify saved result and index"));
  await waitFor(() => expect(operations).toHaveLength(2));
  expect(operations[1]).not.toBe(operations[0]);
  await waitFor(() => expect(screen.queryByText("Saved output still needs review")).toBeNull());
});
