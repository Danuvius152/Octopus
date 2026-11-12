/** Account settings and concurrent webhook target reconciliation. */

import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { SettingsDialog } from "./SettingsDialog";
import { useSessionStore } from "../stores/sessionStore";
import { deferred } from "../test-support/deferred";

beforeEach(() => {
  useSessionStore.getState().setToken("current-account-bearer");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const target = (id: string) => ({ id, type: "webhook", label: `Target ${id}`, config: { url: `https://example.com/${id}` }, enabled: true });

function showNotifications() {
  const tab = screen.getByRole("tab", { name: "Notifications" });
  fireEvent.pointerDown(tab);
  fireEvent.mouseDown(tab);
  fireEvent.click(tab);
}

async function openNotifications() {
  render(<SettingsDialog open onOpenChange={() => {}} onSignOut={vi.fn()} />);
  showNotifications();
}

function removeTarget(id: string) {
  const row = screen.getByText(`Target ${id}`).closest<HTMLElement>(".notifier-item")!;
  fireEvent.click(within(row).getByRole("button", { name: "Delete notifier" }));
}

function fillTarget(id: string) {
  fireEvent.click(screen.getByRole("button", { name: "Add webhook" }));
  fireEvent.change(screen.getByLabelText("Label"), { target: { value: `Target ${id}` } });
  fireEvent.change(screen.getByLabelText("Webhook URL"), { target: { value: `https://example.com/${id}` } });
}

function createTarget(id: string) {
  fillTarget(id);
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
}

it.each([["a", "b"], ["b", "a"]])("two deletions completing %s then %s do not restore a removed target", async (first, second) => {
  const replies = new Map(["a", "b"].map((id) => [id, deferred<Response>()]));
  const fetcher = vi.fn(async (input: string, init?: RequestInit) => {
    if (init?.method === "DELETE") return replies.get(input.split("/").at(-1)!)!.promise;
    return Response.json([target("a"), target("b")]);
  });
  vi.stubGlobal("fetch", fetcher);
  await openNotifications();
  await screen.findByText("Target a");
  removeTarget("a");
  removeTarget("b");
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(2);
  await act(async () => replies.get(first)!.resolve(new Response(null, { status: 204 })));
  await act(async () => replies.get(second)!.resolve(new Response(null, { status: 204 })));
  expect(screen.queryByText("Target a")).toBeNull();
  expect(screen.queryByText("Target b")).toBeNull();
  expect(screen.getByText("No notifier targets configured.")).toBeVisible();
});

it.each(["create-first", "delete-first"])("an add and delete completing %s preserve both confirmed effects", async (order) => {
  const created = deferred<Response>();
  const deleted = deferred<Response>();
  let serverItems = [target("a")];
  vi.stubGlobal("fetch", vi.fn(async (_input: string, init?: RequestInit) => {
    if (init?.method === "POST") return created.promise;
    if (init?.method === "DELETE") return deleted.promise;
    return Response.json(serverItems);
  }));
  await openNotifications();
  await screen.findByText("Target a");
  createTarget("c");
  removeTarget("a");
  const add = () => { serverItems.push(target("c")); created.resolve(Response.json(target("c"), { status: 201 })); };
  const remove = () => { serverItems = serverItems.filter((item) => item.id !== "a"); deleted.resolve(new Response(null, { status: 204 })); };
  await act(async () => { if (order === "create-first") add(); else remove(); });
  await act(async () => { if (order === "create-first") remove(); else add(); });
  expect(screen.queryByText("Target a")).toBeNull();
  expect(screen.getByText("Target c")).toBeVisible();
  expect(document.querySelectorAll(".notifier-item")).toHaveLength(1);
});

it("a GET begun before a confirmed add cannot erase it and schedules one confirmation", async () => {
  const read = deferred<Response>();
  let reads = 0;
  const fetcher = vi.fn(async (_input: string, init?: RequestInit) => {
    if (init?.method === "POST") return Response.json(target("c"), { status: 201 });
    if (++reads === 1) return read.promise;
    return Response.json([target("c")]);
  });
  vi.stubGlobal("fetch", fetcher);
  await openNotifications();
  expect(reads).toBe(1);
  createTarget("c");
  await screen.findByText("Target c");
  await act(async () => read.resolve(Response.json([])));
  expect(screen.getByText("Target c")).toBeVisible();
  await waitFor(() => expect(reads).toBe(2));
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it.each([204, 404])("a late create body cannot restore a target after delete %s and confirmation", async (status) => {
  const read = deferred<Response>();
  const createBody = deferred<unknown>();
  let reads = 0;
  const created = new Response(null, { status: 201 });
  const parseCreate = vi.spyOn(created, "json").mockReturnValue(createBody.promise);
  const fetcher = vi.fn(async (_input: string, init?: RequestInit) => {
    if (init?.method === "POST") return created;
    if (init?.method === "DELETE") return new Response(null, { status });
    if (++reads === 1) return read.promise;
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  await openNotifications();
  expect(reads).toBe(1);
  createTarget("c");
  await waitFor(() => expect(parseCreate).toHaveBeenCalledOnce());
  await act(async () => read.resolve(Response.json([target("c")])));
  removeTarget("c");
  await waitFor(() => expect(screen.queryByText("Target c")).toBeNull());
  await act(async () => createBody.resolve(target("c")));
  expect(screen.queryByText("Target c")).toBeNull();
  await waitFor(() => expect(reads).toBe(status === 204 ? 2 : 3));
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(1);
});

it.each([404, 500, "network"] as const)("a delete %s remains visible with the form closed and confirms without retrying", async (failure) => {
  let reads = 0;
  const fetcher = vi.fn(async (_input: string, init?: RequestInit) => {
    if (init?.method === "DELETE") {
      if (failure === "network") throw new Error("Connection lost");
      return new Response(null, { status: failure });
    }
    return Response.json(++reads === 1 ? [target("a")] : []);
  });
  vi.stubGlobal("fetch", fetcher);
  await openNotifications();
  await screen.findByText("Target a");
  removeTarget("a");
  expect(await screen.findByRole("alert")).toHaveTextContent(failure === "network" ? "Connection lost" : `Server returned ${failure}.`);
  await waitFor(() => expect(screen.queryByText("Target a")).toBeNull());
  expect(screen.getByRole("alert")).toBeVisible();
  expect(screen.getByRole("button", { name: "Add webhook" })).toBeVisible();
  expect(reads).toBe(2);
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(1);
});

it.each([500, "network"] as const)("a create %s confirms the saved target without resending or hiding the error", async (failure) => {
  let reads = 0;
  const fetcher = vi.fn(async (_input: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      if (failure === "network") throw new Error("Connection lost");
      return new Response(null, { status: failure });
    }
    return Response.json(++reads === 1 ? [] : [target("c")]);
  });
  vi.stubGlobal("fetch", fetcher);
  await openNotifications();
  await waitFor(() => expect(reads).toBe(1));
  createTarget("c");
  expect(await screen.findByRole("alert")).toHaveTextContent(failure === "network" ? "Connection lost" : `Server returned ${failure}.`);
  expect(await screen.findByText("Target c")).toBeVisible();
  expect(screen.getByLabelText("Label")).toHaveValue("Target c");
  expect(screen.getByRole("alert")).toBeVisible();
  expect(reads).toBe(2);
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it("guards duplicate deletion synchronously while other targets remain usable", async () => {
  const deletion = deferred<Response>();
  const fetcher = vi.fn(async (_input: string, init?: RequestInit) => {
    if (init?.method === "DELETE") return deletion.promise;
    return Response.json([target("a"), target("b")]);
  });
  vi.stubGlobal("fetch", fetcher);
  await openNotifications();
  await screen.findByText("Target a");
  const button = within(screen.getByText("Target a").closest<HTMLElement>(".notifier-item")!).getByRole("button");
  act(() => { fireEvent.click(button); fireEvent.click(button); });
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(1);
  expect(button).toBeDisabled();
  expect(within(screen.getByText("Target b").closest<HTMLElement>(".notifier-item")!).getByRole("button")).toBeEnabled();
  await act(async () => deletion.resolve(new Response(null, { status: 204 })));
  expect(screen.queryByText("Target a")).toBeNull();
  expect(screen.getByText("Target b")).toBeVisible();
});

it("guards duplicate creation synchronously and keeps the submitted form stable until success", async () => {
  const creation = deferred<Response>();
  const fetcher = vi.fn(async (_input: string, init?: RequestInit) => {
    if (init?.method === "POST") return creation.promise;
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  await openNotifications();
  await screen.findByText("No notifier targets configured.");
  fillTarget("c");
  const save = screen.getByRole("button", { name: "Save" });
  // Exercise two handlers within the same render, before disabled is committed.
  act(() => { fireEvent.click(save); fireEvent.click(save); });
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  expect(save).toBeDisabled();
  expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
  expect(screen.getByLabelText("Label")).toBeDisabled();
  expect(screen.getByLabelText("Webhook URL")).toBeDisabled();
  await act(async () => creation.resolve(Response.json(target("c"), { status: 201 })));
  expect(screen.getByText("Target c")).toBeVisible();
  expect(screen.getByRole("button", { name: "Add webhook" })).toBeEnabled();
});

it.each(["GET", "POST", "DELETE"])("switching account retires the old %s receipt without changing the new list or draft", async (method) => {
  const oldHeaders = deferred<Response>();
  const oldBody = deferred<unknown>();
  const oldResponse = new Response(null, { status: method === "POST" ? 201 : 200 });
  const parseOld = vi.spyOn(oldResponse, "json").mockReturnValue(oldBody.promise);
  const fetcher = vi.fn(async (_input: string, init?: RequestInit) => {
    if (new Headers(init?.headers).get("Authorization") === "Bearer new-account") return Response.json([target("b")]);
    if ((init?.method ?? "GET") === method) {
      if (method === "DELETE") return oldHeaders.promise;
      return oldResponse;
    }
    return Response.json([target("a")]);
  });
  vi.stubGlobal("fetch", fetcher);
  await openNotifications();
  if (method !== "GET") {
    await screen.findByText("Target a");
    if (method === "POST") createTarget("c"); else removeTarget("a");
  }
  if (method !== "DELETE") await waitFor(() => expect(parseOld).toHaveBeenCalledOnce());
  act(() => useSessionStore.getState().setToken("new-account"));
  await screen.findByText("Target b");
  expect(screen.queryByText("Target a")).toBeNull();
  fillTarget("new-draft");
  await act(async () => {
    if (method === "DELETE") oldHeaders.resolve(new Response(null, { status: 204 }));
    else oldBody.resolve(method === "GET" ? [target("a")] : target("c"));
  });
  expect(screen.getByText("Target b")).toBeVisible();
  expect(screen.queryByText("Target c")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByLabelText("Label")).toHaveValue("Target new-draft");
  expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  expect(fetcher.mock.calls.filter(([, init]) => new Headers(init?.headers).get("Authorization") === "Bearer new-account")).toHaveLength(1);
});

it("coalesces overlapping confirmation needs and rejects a body older than a successful mutation", async () => {
  const body = deferred<unknown>();
  let reads = 0;
  const oldSnapshot = new Response(null);
  const parseSnapshot = vi.spyOn(oldSnapshot, "json").mockReturnValue(body.promise);
  const fetcher = vi.fn(async (_input: string, init?: RequestInit) => {
    if (init?.method === "POST") return Response.json(target("d"), { status: 201 });
    if (init?.method === "DELETE") return new Response(null, { status: 404 });
    reads++;
    if (reads === 1) return Response.json([target("a"), target("b"), target("c")]);
    if (reads === 2) return oldSnapshot;
    return Response.json([target("d")]);
  });
  vi.stubGlobal("fetch", fetcher);
  await openNotifications();
  await screen.findByText("Target a");
  removeTarget("a");
  await waitFor(() => expect(parseSnapshot).toHaveBeenCalledOnce());
  removeTarget("b");
  removeTarget("c");
  await waitFor(() => expect(fetcher.mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(3));
  createTarget("d");
  await screen.findByText("Target d");
  expect(reads).toBe(2);
  await act(async () => body.resolve([target("b"), target("c")]));
  expect(screen.getByText("Target d")).toBeVisible();
  expect(screen.queryByText("Target a")).toBeNull();
  expect(screen.queryByText("Target b")).toBeNull();
  expect(screen.queryByText("Target c")).toBeNull();
  expect(reads).toBe(3);
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it("a failed confirmation stays visible and ends the read round despite queued needs", async () => {
  const read = deferred<Response>();
  let reads = 0;
  const fetcher = vi.fn(async (_input: string, init?: RequestInit) => {
    if (init?.method === "DELETE") return new Response(null, { status: 404 });
    if (++reads === 1) return Response.json([target("a"), target("b")]);
    if (reads === 2) return read.promise;
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetcher);
  await openNotifications();
  await screen.findByText("Target a");
  removeTarget("a");
  await waitFor(() => expect(reads).toBe(2));
  removeTarget("b");
  await act(async () => read.reject(new Error("Cannot confirm targets")));
  expect(screen.getByRole("alert")).toHaveTextContent("Cannot confirm targets");
  expect(screen.getByRole("alert")).toHaveTextContent("Server returned 404.");
  expect(screen.getByText("Target a")).toBeVisible();
  expect(screen.getByText("Target b")).toBeVisible();
  expect(reads).toBe(2);
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(2);
});

it.each(["GET", "POST"])("unmounting retires the old %s without starting confirmation in a replacement panel", async (method) => {
  const oldReply = deferred<Response>();
  let reads = 0;
  const fetcher = vi.fn(async (_input: string, init?: RequestInit) => {
    if (init?.method === "DELETE") return new Response(null, { status: 204 });
    if (method === "POST" && init?.method === "POST") return oldReply.promise;
    if (init?.method === "POST") return Response.json(target("c"), { status: 201 });
    if (++reads === 1 && method === "GET") return oldReply.promise;
    return Response.json(reads === 1 ? [target("a")] : [target("b")]);
  });
  vi.stubGlobal("fetch", fetcher);
  await openNotifications();
  if (method === "POST") await screen.findByText("Target a");
  createTarget("c");
  if (method === "GET") await screen.findByText("Target c");
  else {
    removeTarget("a");
    await waitFor(() => expect(screen.queryByText("Target a")).toBeNull());
  }
  cleanup();
  await openNotifications();
  await screen.findByText("Target b");
  fillTarget("new-draft");
  await act(async () => oldReply.resolve(Response.json(method === "GET" ? [] : target("c"), { status: method === "GET" ? 200 : 201 })));
  expect(screen.getByText("Target b")).toBeVisible();
  expect(screen.queryByText("Target c")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByLabelText("Label")).toHaveValue("Target new-draft");
  expect(reads).toBe(2);
});

it("a failed deletion releases pending state and permits a deliberate retry", async () => {
  let deletions = 0;
  const fetcher = vi.fn(async (_input: string, init?: RequestInit) => {
    if (init?.method === "DELETE") return new Response(null, { status: ++deletions === 1 ? 500 : 204 });
    return Response.json([target("a")]);
  });
  vi.stubGlobal("fetch", fetcher);
  await openNotifications();
  await screen.findByText("Target a");
  removeTarget("a");
  await screen.findByRole("alert");
  await waitFor(() => expect(within(screen.getByText("Target a").closest<HTMLElement>(".notifier-item")!).getByRole("button")).toBeEnabled());
  expect(deletions).toBe(1);
  removeTarget("a");
  await waitFor(() => expect(screen.queryByText("Target a")).toBeNull());
  expect(screen.queryByRole("alert")).toBeNull();
  expect(deletions).toBe(2);
});

it("a failed creation preserves the form and permits a deliberate retry", async () => {
  let creations = 0;
  vi.stubGlobal("fetch", vi.fn(async (_input: string, init?: RequestInit) => {
    if (init?.method === "POST") return ++creations === 1 ? new Response(null, { status: 500 }) : Response.json(target("c"), { status: 201 });
    return Response.json([]);
  }));
  await openNotifications();
  await screen.findByText("No notifier targets configured.");
  createTarget("c");
  await screen.findByRole("alert");
  expect(screen.getByLabelText("Label")).toHaveValue("Target c");
  expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  expect(creations).toBe(1);
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(await screen.findByText("Target c")).toBeVisible();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByRole("button", { name: "Add webhook" })).toBeEnabled();
  expect(creations).toBe(2);
});

it.each(["http", "json"])("an initial GET %s error is visible and does not refetch automatically", async (failure) => {
  const response = new Response(null, { status: failure === "http" ? 503 : 200 });
  vi.spyOn(response, "json").mockRejectedValue(new Error("Invalid targets"));
  const fetcher = vi.fn(async () => response);
  vi.stubGlobal("fetch", fetcher);
  await openNotifications();
  expect(await screen.findByRole("alert")).toHaveTextContent(failure === "http" ? "Server returned 503." : "Invalid targets");
  expect(fetcher).toHaveBeenCalledOnce();
});

it("effect replay cannot install a retired initial read over the active read", async () => {
  const oldRead = deferred<Response>();
  const fetcher = vi.fn().mockReturnValueOnce(oldRead.promise).mockResolvedValue(Response.json([target("b")]));
  vi.stubGlobal("fetch", fetcher);
  render(<StrictMode><SettingsDialog open onOpenChange={() => {}} onSignOut={vi.fn()} /></StrictMode>);
  showNotifications();
  await screen.findByText("Target b");
  await act(async () => oldRead.resolve(Response.json([target("a")])));
  expect(screen.getByText("Target b")).toBeVisible();
  expect(screen.queryByText("Target a")).toBeNull();
  expect(fetcher).toHaveBeenCalledTimes(2);
});

it("keeps the current bearer and sign out without installation rotation", () => {
  const signOut = vi.fn();
  render(<SettingsDialog open onOpenChange={() => {}} onSignOut={signOut} />);
  const tab = screen.getByRole("tab", { name: /account/i });
  fireEvent.pointerDown(tab);
  fireEvent.mouseDown(tab);
  fireEvent.click(tab);
  expect(screen.queryByPlaceholderText(/New token/i)).toBeNull();
  expect(screen.getByText("current-account-bearer")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
  expect(signOut).toHaveBeenCalledOnce();
});
