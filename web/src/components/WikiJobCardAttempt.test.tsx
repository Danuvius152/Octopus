import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { WikiJobCards } from "./WikiJobCards";
import { useSessionStore } from "../stores/sessionStore";
import { wikiJob } from "../lib/wikiJobs.fixture";

beforeEach(() => useSessionStore.setState({
  token: "token-a", wikiJobs: {}, wikiJobDirty: {}, dispatchGeneration: "service-a", connected: true,
}));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("keeps a newly running task controllable when its old queued detail response arrives late", async () => {
  const queued = wikiJob({ status: "queued", attempt: 0, state_version: 1 });
  const running = wikiJob({ status: "running", attempt: 1, state_version: 2 });
  let finish!: (response: Response) => void;
  let latest = queued;
  const fetcher = vi.fn((url: string) => {
    if (String(url).includes("/details")) return new Promise<Response>((resolve) => { finish = resolve; });
    return Promise.resolve(Response.json([latest]));
  });
  vi.stubGlobal("fetch", fetcher);
  useSessionStore.getState().mergeWikiJobs("original", [queued]);
  render(<WikiJobCards sessionId="original" />);
  fireEvent.click(screen.getByRole("button", { name: "Task details" }));
  expect(finish).toEqual(expect.any(Function));
  await act(async () => {
    latest = running;
    useSessionStore.getState().mergeWikiJobs("original", [running]);
  });
  await act(async () => { finish(Response.json({ detail: "Maintenance attempt changed" }, { status: 409 })); });
  expect(screen.getByText(/Wiki maintenance · Reviewing/)).toBeTruthy();
  expect(screen.getByRole("button", { name: "Cancel maintenance" })).toBeTruthy();
  expect(screen.queryByText("Task details are unavailable. The saved summary remains here.")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Close details" }));
  expect(screen.getByRole("button", { name: "Task details" })).toBeTruthy();
  expect(fetcher.mock.calls.every(([url]) => !String(url).includes("/cancel"))).toBe(true);
});
