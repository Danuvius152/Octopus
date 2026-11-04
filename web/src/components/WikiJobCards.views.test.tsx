import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { WikiJobCards } from "./WikiJobCards";
import { useSessionStore } from "../stores/sessionStore";
import { wikiJob } from "../lib/wikiJobs.fixture";

function deferred() {
  let resolve!: (value: Response) => void;
  const promise = new Promise<Response>((yes) => { resolve = yes; });
  return { promise, resolve };
}
function MountedChat() {
  const view = useSessionStore((state) => state.mainView);
  return <div hidden={view !== "chat"}><WikiJobCards sessionId="original" /></div>;
}
async function navigate(mainView: "chat" | "schedules") {
  await act(async () => useSessionStore.setState({ mainView }));
}
const page = () => Array.from({ length: 30 }, (_, index) => wikiJob({ job_id: `job-${index}`, status: "completed" }));

beforeEach(() => {
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useSessionStore.setState({ token: "token-a", activeSessionId: "original", dispatchGeneration: "service-a", connected: true });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("rebuilds pagination after leaving the still-mounted chat during a page read", async () => {
  const old = deferred();
  let pages = 0;
  let firstPages = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (new URL(url).searchParams.get("offset") === "30") {
      pages++;
      return pages === 1 ? old.promise : Response.json([wikiJob({ job_id: "older", status: "completed" })]);
    }
    firstPages++;
    return Response.json(page());
  }));
  render(<MountedChat />);
  fireEvent.click(await screen.findByRole("button", { name: "More maintenance tasks" }));
  await waitFor(() => expect(pages).toBe(1));
  await navigate("schedules");
  const hiddenRequests = firstPages;
  await navigate("chat");
  await waitFor(() => expect(firstPages).toBeGreaterThan(hiddenRequests));
  const more = await screen.findByRole("button", { name: "More maintenance tasks" });
  expect(more).toBeEnabled();
  fireEvent.click(more);
  await screen.findByRole("button", { name: "Completed maintenance (31 loaded)" });
  await act(async () => old.resolve(Response.json([wikiJob({ job_id: "late", status: "completed" })])));
  expect(useSessionStore.getState().wikiJobs.original.some((card) => card.job_id === "late")).toBe(false);
  expect(pages).toBe(2);
});

it("restores pagination from the count returned by a manual recovery", async () => {
  let first = true;
  const offsets: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const offset = new URL(url).searchParams.get("offset")!;
    offsets.push(offset);
    if (first) { first = false; return Response.json({}, { status: 503 }); }
    return Response.json(offset === "30" ? [wikiJob({ job_id: "older", status: "completed" })] : page());
  }));
  render(<MountedChat />);
  fireEvent.click(await screen.findByRole("button", { name: "Refresh task state" }));
  fireEvent.click(await screen.findByRole("button", { name: "More maintenance tasks" }));
  await screen.findByRole("button", { name: "Completed maintenance (31 loaded)" });
  expect(offsets).toEqual(["0", "0", "30"]);
});

it("reopens a stalled detail observation after the mounted chat becomes visible", async () => {
  const old = deferred();
  const card = wikiJob();
  let details = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (!url.includes("/details")) return Response.json([card]);
    if (++details === 1) return old.promise;
    return Response.json({ job_id: card.job_id, vault_id: card.vault_id, state_version: 1,
      attempt: 1, current_attempt: 1, attempts: [1], status: "running", settlement_phase: "none",
      error: "Original evidence", items: [], outputs: [], next_offset: null,
      actions: { verify: true, retry: false, readmit: false, blocked_reason: null } });
  }));
  render(<MountedChat />);
  fireEvent.click(await screen.findByRole("button", { name: "Task details" }));
  await waitFor(() => expect(details).toBe(1));
  await navigate("schedules");
  expect(details).toBe(1);
  await navigate("chat");
  await screen.findByText("Original evidence");
  expect(screen.getByRole("button", { name: "Verify saved result and index" })).toBeEnabled();
  await act(async () => old.resolve(Response.json({ detail: "old unavailable" }, { status: 404 })));
  expect(screen.getByRole("dialog")).toBeTruthy();
  expect(details).toBe(2);
});

it.each(["hidden", "returned"])("retires original POST feedback when it returns while %s, without replay", async (timing) => {
  const post = deferred();
  const card = wikiJob();
  let posts = 0;
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "POST") { posts++; return post.promise; }
    reads++;
    return Response.json([card]);
  }));
  render(<MountedChat />);
  const cancel = await screen.findByRole("button", { name: "Cancel maintenance" });
  fireEvent.click(cancel);
  await waitFor(() => expect(posts).toBe(1));
  await navigate("schedules");
  if (timing === "hidden") await act(async () => post.resolve(Response.json({}, { status: 403 })));
  await navigate("chat");
  await waitFor(() => expect(screen.getByRole("button", { name: "Cancel maintenance" })).toBeEnabled());
  const currentReads = reads;
  if (timing === "returned") await act(async () => post.resolve(Response.json({}, { status: 403 })));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(posts).toBe(1);
  expect(reads).toBe(currentReads);
});

it("preserves an unobserved detail control identity through a trip to another page", async () => {
  const post = deferred();
  const card = wikiJob({ status: "completed_with_attention" });
  const controls: Record<string, unknown>[] = [];
  let details = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      controls.push(JSON.parse(String(init.body)));
      return controls.length === 1 ? post.promise : Response.json({});
    }
    if (!url.includes("/details")) return Response.json([card]);
    details++;
    return Response.json({ job_id: card.job_id, vault_id: card.vault_id, state_version: 1,
      attempt: 1, current_attempt: 1, attempts: [1], status: card.status, settlement_phase: "none",
      error: "Original evidence", items: [], outputs: [], next_offset: null,
      actions: { verify: true, retry: false, readmit: false, blocked_reason: null } });
  }));
  render(<MountedChat />);
  fireEvent.click(await screen.findByRole("button", { name: "Task details" }));
  const label = "Verify saved result and index";
  const verify = await screen.findByRole("button", { name: label });
  await waitFor(() => expect(verify).toBeEnabled());
  fireEvent.click(verify);
  await waitFor(() => expect(controls).toHaveLength(1));
  await navigate("schedules");
  await navigate("chat");
  await waitFor(() => expect(screen.getByRole("button", { name: label })).toBeEnabled());
  const currentDetails = details;
  await act(async () => post.resolve(Response.json({})));
  expect(screen.queryByText("Verification request accepted for attempt 1.")).toBeNull();
  expect(controls).toHaveLength(1);
  expect(details).toBe(currentDetails);
  // This is a new explicit click. The unobserved response did not discard the
  // original operation ID or replay it automatically while the page was hidden.
  fireEvent.click(screen.getByRole("button", { name: label }));
  await screen.findByText("Verification request accepted for attempt 1.");
  expect(controls).toHaveLength(2);
  expect(controls[1]).toEqual(controls[0]);
});
