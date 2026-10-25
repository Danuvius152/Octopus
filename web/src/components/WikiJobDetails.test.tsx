import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { WikiJobDetails } from "./WikiJobDetails";
import { wikiJob } from "../lib/wikiJobs.fixture";
import { useSessionStore } from "../stores/sessionStore";

const unavailable = vi.fn();
const close = () => {};
const detail = (overrides: Record<string, unknown> = {}) => ({
  job_id: "job-1", vault_id: "vault-1", state_version: 1,
  attempt: 1, current_attempt: 1, attempts: [1], status: "running",
  settlement_phase: "none", error: null, items: [], outputs: [], next_offset: null,
  actions: { verify: false, retry: false, readmit: false, blocked_reason: null },
  ...overrides,
});
const saved = { receipt_id: "receipt", path: "Library/note.md", operation: "publish", status: "applied", reason: "Saved" };
function deferred() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((yes) => { resolve = yes; });
  return { promise, resolve };
}

beforeEach(() => {
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useSessionStore.setState({ token: "test-token", dispatchGeneration: "test-service" });
  unavailable.mockClear();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("refreshes open details at the card version boundary and ignores an older response", async () => {
  const old = deferred();
  const fetcher = vi.fn().mockImplementationOnce(() => old.promise).mockResolvedValue(Response.json(detail({
    status: "completed", state_version: 2, outputs: [saved],
    actions: { verify: true, retry: false, readmit: false, blocked_reason: null },
  })));
  vi.stubGlobal("fetch", fetcher);
  const view = render(<WikiJobDetails card={wikiJob({ state_version: 1 })} sessionId="s" close={close} setUnavailable={unavailable} />);
  view.rerender(<WikiJobDetails card={wikiJob({ status: "completed", state_version: 2 })} sessionId="s" close={close} setUnavailable={unavailable} />);
  await screen.findByText("Historical output");
  expect(screen.getByText("Verify saved result and index")).toBeTruthy();
  await act(async () => old.resolve(Response.json(detail({ error: "stale failure" }))));
  expect(screen.queryByText("stale failure")).toBeNull();
  expect(screen.getByText("Historical output")).toBeTruthy();
  expect(fetcher).toHaveBeenCalledTimes(2);
});

it("follows the latest attempt until the user explicitly selects a historical attempt", async () => {
  const fetcher = vi.fn(async (url: string) => Response.json(detail({
    attempt: Number(new URL(url).searchParams.get("attempt")), current_attempt: 3, attempts: [1, 2, 3],
  })));
  vi.stubGlobal("fetch", fetcher);
  const view = render(<WikiJobDetails card={wikiJob({ attempt: 1 })} sessionId="s" close={close} setUnavailable={unavailable} />);
  await screen.findByLabelText("Maintenance attempt");
  view.rerender(<WikiJobDetails card={wikiJob({ attempt: 2, state_version: 2 })} sessionId="s" close={close} setUnavailable={unavailable} />);
  await waitFor(() => expect(screen.getByLabelText("Maintenance attempt")).toHaveValue("2"));
  fireEvent.change(screen.getByLabelText("Maintenance attempt"), { target: { value: "1" } });
  await waitFor(() => expect(screen.getByLabelText("Maintenance attempt")).toHaveValue("1"));
  view.rerender(<WikiJobDetails card={wikiJob({ attempt: 3, state_version: 3 })} sessionId="s" close={close} setUnavailable={unavailable} />);
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(4));
  await waitFor(() => expect(screen.getByLabelText("Maintenance attempt")).toHaveValue("1"));
  expect(new URL(fetcher.mock.calls[3][0]).searchParams.get("attempt")).toBe("1");
});

it("does not let an old evidence page replace refreshed details or mark them unavailable", async () => {
  const page = deferred();
  const fetcher = vi.fn(async (url: string) => {
    if (new URL(url).searchParams.get("offset") === "1") return page.promise;
    return Response.json(detail({ next_offset: 1 }));
  });
  vi.stubGlobal("fetch", fetcher);
  const view = render(<WikiJobDetails card={wikiJob()} sessionId="s" close={close} setUnavailable={unavailable} />);
  fireEvent.click(await screen.findByText("More task evidence"));
  fetcher.mockImplementation(async () => Response.json(detail({ state_version: 2, outputs: [saved] })));
  view.rerender(<WikiJobDetails card={wikiJob({ state_version: 2 })} sessionId="s" close={close} setUnavailable={unavailable} />);
  await screen.findByText("Historical output");
  await act(async () => page.resolve(Response.json({ detail: "gone" }, { status: 404 })));
  expect(screen.getByText("Historical output")).toBeTruthy();
  expect(unavailable).not.toHaveBeenCalled();
});

it.each([
  ["verify", "Verify saved result and index"],
  ["retry", "Continue unfinished work"],
  ["readmit", "Re-admit selected scope"],
])("submits %s without randomUUID and reuses its identity after a lost response", async (action, button) => {
  const random = vi.fn((bytes: Uint8Array) => {
    bytes.fill(17);
    return bytes;
  });
  vi.stubGlobal("crypto", { getRandomValues: random });
  const card = wikiJob({ status: "failed" });
  const posts: Record<string, unknown>[] = [];
  const paths: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      paths.push(url);
      posts.push(JSON.parse(String(init.body)));
      if (posts.length === 1) throw new TypeError("lost response");
      return Response.json({});
    }
    if (url.includes("/details")) return Response.json(detail({
      actions: { verify: true, retry: true, readmit: true, blocked_reason: null },
    }));
    return Response.json([card]);
  }));
  render(<WikiJobDetails card={card} sessionId="s" close={close} setUnavailable={unavailable} />);
  fireEvent.click(await screen.findByText(button));
  await screen.findByText("lost response");
  fireEvent.click(screen.getByText(button));
  await waitFor(() => expect(posts).toHaveLength(2));
  expect(posts[0]).toEqual(posts[1]);
  expect(paths.every((path) => path.endsWith(`/${action}`))).toBe(true);
  expect(posts[0].operation_id).toMatch(/^[a-f\d-]{36}$/);
  expect(random).toHaveBeenCalledTimes(1);
});

it("shows operation ID failures in the dialog", async () => {
  vi.stubGlobal("crypto", {});
  const fetcher = vi.fn(async () => Response.json(detail({
    actions: { verify: true, retry: false, readmit: false, blocked_reason: null },
  })));
  vi.stubGlobal("fetch", fetcher);
  render(<WikiJobDetails card={wikiJob()} sessionId="s" close={close} setUnavailable={unavailable} />);
  fireEvent.click(await screen.findByText("Verify saved result and index"));
  expect((await screen.findByRole("alert")).textContent).toContain("Secure randomness is unavailable");
  expect(fetcher).toHaveBeenCalledTimes(1);
});
