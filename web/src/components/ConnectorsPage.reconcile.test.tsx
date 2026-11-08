import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ConnectorInstallationInfo } from "../api";
import { useSessionStore } from "../stores/sessionStore";
import { ConnectorsPage } from "./ConnectorsPage";
import { SidebarAgents } from "./SidebarAgents";
import { SidebarManage } from "./SidebarManage";
import { AgentFormPage } from "./AgentFormPage";

const installed = (id = "old", kind = "fake"): ConnectorInstallationInfo => ({
  id, kind, label: `${id} account`, auth_type: "oauth", external_account_id: id,
  scopes: [], enable_by_default: false, needs_reconnect: false,
  token_expires_at: null, last_refresh_error_code: null, created_at: "now",
});
const deferred = <T = Response,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((finish) => { resolve = finish; });
  return { promise, resolve };
};
function otherRequest(path: string): Response {
  if (path === "/api/connectors/catalog") return Response.json([{ kind: "fake", display_name: "Fake", available: true }]);
  if (path === "/api/backends") return Response.json({ available: ["codex"] });
  return Response.json([]);
}
function page() {
  return render(<><ConnectorsPage onToggleSidebar={() => {}} /><SidebarManage /></>);
}
beforeEach(() => useSessionStore.setState({
  ...useSessionStore.getInitialState(), token: "connector-account",
  accountGeneration: useSessionStore.getState().accountGeneration + 1, dispatchGeneration: "service-a",
  connectorInstallations: [installed()],
}));
afterEach(() => {
  cleanup();
  useSessionStore.getState().setToken("");
  vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks();
});

it.each(["page", "sidebar"])("a late %s GET cannot restore a confirmed disconnected card or sidebar count", async (origin) => {
  const old = deferred();
  let reads = 0;
  let deleted = false;
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    if (init?.method === "DELETE") { deleted = true; return Promise.resolve(new Response(null, { status: 204 })); }
    if (path === "/api/connectors") return ++reads === 1 ? old.promise : Promise.resolve(Response.json(deleted ? [] : [installed()]));
    return Promise.resolve(otherRequest(path));
  }));
  if (origin === "sidebar") render(<SidebarAgents />);
  const view = page();
  await waitFor(() => expect(reads).toBeGreaterThan(0));
  fireEvent.click(screen.getByRole("button", { name: "Disconnect old account" }));
  await waitFor(() => expect(screen.queryByText("old account")).toBeNull());
  await act(async () => old.resolve(Response.json([installed()])));
  expect(screen.queryByText("old account")).toBeNull();
  expect(view.container.querySelector(".btn-manage-connectors .manage-summary")).toHaveTextContent(/^0$/);
});

it("keeps the connected card and displays a refused disconnect", async () => {
  const fetcher = vi.fn((url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    if (init?.method === "DELETE") return Promise.resolve(new Response(null, { status: 403 }));
    return Promise.resolve(path === "/api/connectors" ? Response.json([installed()]) : otherRequest(path));
  });
  vi.stubGlobal("fetch", fetcher);
  page();
  fireEvent.click(screen.getByRole("button", { name: "Disconnect old account" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not disconnect: HTTP 403");
  expect(screen.getByText("old account")).toBeVisible();
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(1);
});

it("deleting a custom definition removes all its installations and retires old list reads", async () => {
  const old = deferred();
  let deleted = false;
  const rows = [installed(), installed("second"), installed("other", "github")];
  useSessionStore.setState({ connectorInstallations: rows });
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    if (path === "/api/connectors/custom/fake" && init?.method === "DELETE") {
      deleted = true; return Promise.resolve(new Response(null, { status: 204 }));
    }
    if (path === "/api/connectors/catalog") return Promise.resolve(Response.json(deleted ? [] : [
      { kind: "fake", display_name: "Fake", available: true, custom: true },
    ]));
    if (path === "/api/connectors") return deleted ? Promise.resolve(Response.json([rows[2]])) : old.promise;
    return Promise.resolve(otherRequest(path));
  }));
  const view = page();
  fireEvent.click(view.container.querySelector(".btn-connector-add")!);
  fireEvent.click(await screen.findByTitle("Remove connector"));
  await waitFor(() => expect(screen.queryByTitle("Remove connector")).toBeNull());
  expect(screen.queryByText("old account")).toBeNull();
  expect(screen.queryByText("second account")).toBeNull();
  await act(async () => old.resolve(Response.json(rows)));
  expect(screen.queryByText("old account")).toBeNull();
  expect(screen.getByText("other account")).toBeInTheDocument();
  expect(view.container.querySelector(".btn-manage-connectors .manage-summary")).toHaveTextContent(/^1$/);
});

it.each(["poll", "cancel"])("a %s success confirmation superseded by disconnect still discovers the new connection", async (completion) => {
  const confirmation = deferred();
  const added = installed("new");
  let reads = 0;
  vi.spyOn(window, "open").mockReturnValue(null);
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    if (path === "/api/connectors/oauth/start") return Promise.resolve(Response.json({ login_id: "login", authorize_url: "https://fake.test/auth" }));
    if (path === "/api/connectors/oauth/cancel" || path.includes("/oauth/status/")) {
      return Promise.resolve(Response.json({ status: "success", installation_id: "new" }));
    }
    if (init?.method === "DELETE") return Promise.resolve(new Response(null, { status: 204 }));
    if (path === "/api/connectors") {
      reads++;
      return reads === 2 ? confirmation.promise : Promise.resolve(Response.json(reads === 1 ? [installed()] : [added]));
    }
    return Promise.resolve(otherRequest(path));
  }));
  const view = page();
  fireEvent.click(view.container.querySelector(".btn-connector-add")!);
  fireEvent.click(await screen.findByRole("button", { name: "Connect" }));
  await screen.findByText("Waiting for sign-in in the opened tab…");
  if (completion === "cancel") fireEvent.click(screen.getByRole("button", { name: "Close" }));
  await waitFor(() => expect(reads).toBe(2), { timeout: 2500 });
  // A disconnect can finish from another view while the dialog waits for GET.
  fireEvent.click(view.container.querySelector(".btn-connector-delete")!);
  await waitFor(() => expect(screen.queryByText("old account")).toBeNull());
  await screen.findByText("new account");
  await act(async () => confirmation.resolve(Response.json([installed(), added])));
  expect(screen.queryByText("old account")).toBeNull();
  expect(screen.getByText("new account")).toBeVisible();
  expect(screen.queryByRole("dialog")).toBeNull();
});

it.each(["old", "second"])("concurrent disconnects both apply when %s finishes first", async (first) => {
  const pending = { old: deferred(), second: deferred() };
  let rows = [installed(), installed("second")];
  useSessionStore.setState({ connectorInstallations: rows });
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    if (init?.method === "DELETE") return pending[path.endsWith("/old") ? "old" : "second"].promise;
    return Promise.resolve(path === "/api/connectors" ? Response.json(rows) : otherRequest(path));
  }));
  const view = page();
  fireEvent.click(screen.getByRole("button", { name: "Disconnect old account" }));
  fireEvent.click(screen.getByRole("button", { name: "Disconnect second account" }));
  for (const id of [first, first === "old" ? "second" : "old"] as const) {
    rows = rows.filter((row) => row.id !== id);
    await act(async () => pending[id as keyof typeof pending].resolve(new Response(null, { status: 204 })));
    expect(screen.queryByText(`${id} account`)).toBeNull();
  }
  expect(screen.queryByText("old account")).toBeNull();
  expect(screen.queryByText("second account")).toBeNull();
  expect(view.container.querySelector(".btn-manage-connectors .manage-summary")).toHaveTextContent(/^0$/);
});

it("retires a GET started during DELETE and keeps its successor when the old GET finishes", async () => {
  const deletion = deferred(), overlapping = deferred(), confirmation = deferred();
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    if (init?.method === "DELETE") return deletion.promise;
    if (path === "/api/connectors") {
      reads++;
      return reads === 1 ? Promise.resolve(Response.json([installed()])) : reads === 2 ? overlapping.promise : confirmation.promise;
    }
    return Promise.resolve(otherRequest(path));
  }));
  page();
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Disconnect old account" }));
  render(<SidebarAgents />);
  await waitFor(() => expect(reads).toBe(2));
  await act(async () => deletion.resolve(new Response(null, { status: 204 })));
  expect(reads).toBe(3);
  await act(async () => overlapping.resolve(Response.json([installed()])));
  expect(screen.queryByText("old account")).toBeNull();
  render(<SidebarAgents />);
  expect(reads).toBe(3); // The successor is still the shared active observation.
  await act(async () => confirmation.resolve(Response.json([installed("new")])));
  expect(screen.getByText("new account")).toBeVisible();
  expect(screen.queryByText("old account")).toBeNull();
});

it("does not restore an old account read after switching away and back to the same token", async () => {
  const old = deferred(), current = deferred();
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    const path = new URL(url).pathname;
    if (path === "/api/connectors") return ++reads === 1 ? old.promise : current.promise;
    return Promise.resolve(otherRequest(path));
  }));
  page();
  act(() => {
    useSessionStore.getState().setToken("other-account");
    useSessionStore.getState().setToken("connector-account");
  });
  await waitFor(() => expect(reads).toBe(2));
  await act(async () => old.resolve(Response.json([installed("old-identity")])));
  await act(async () => current.resolve(Response.json([installed("current-identity")])));
  expect(screen.queryByText("old-identity account")).toBeNull();
  expect(screen.getByText("current-identity account")).toBeVisible();
});

it("a completed connection remains successful when list synchronization fails, and refresh recovers", async () => {
  let reads = 0;
  vi.spyOn(window, "open").mockReturnValue(null);
  const fetcher = vi.fn((url: string) => {
    const path = new URL(url).pathname;
    if (path === "/api/connectors/oauth/start") return Promise.resolve(Response.json({ login_id: "login", authorize_url: "https://fake.test/auth" }));
    if (path === "/api/connectors/oauth/cancel") return Promise.resolve(Response.json({ status: "success", installation_id: "new" }));
    if (path === "/api/connectors") {
      reads++;
      return Promise.resolve(reads === 2 ? new Response(null, { status: 503 }) : Response.json(reads === 1 ? [installed()] : [installed(), installed("new")]));
    }
    return Promise.resolve(otherRequest(path));
  });
  vi.stubGlobal("fetch", fetcher);
  const view = page();
  fireEvent.click(view.container.querySelector(".btn-connector-add")!);
  fireEvent.click(await screen.findByRole("button", { name: "Connect" }));
  await screen.findByText("Waiting for sign-in in the opened tab…");
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(screen.getByRole("alert")).toHaveTextContent("Connections not synchronized: HTTP 503");
  expect(screen.getByText("old account")).toBeVisible();
  expect(view.container.querySelector(".btn-manage-connectors .manage-summary")).toHaveTextContent("not synchronized");
  fireEvent.click(screen.getByRole("button", { name: "Refresh connections" }));
  expect(await screen.findByText("new account")).toBeVisible();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(fetcher.mock.calls.filter(([url]) => url.endsWith("/oauth/start"))).toHaveLength(1);
});

it("a refused custom definition deletion keeps cards and reports the error in the open dialog", async () => {
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    if (init?.method === "DELETE") return Promise.resolve(new Response(null, { status: 403 }));
    if (path === "/api/connectors/catalog") return Promise.resolve(Response.json([
      { kind: "fake", display_name: "Fake", available: true, custom: true },
    ]));
    return Promise.resolve(path === "/api/connectors" ? Response.json([installed()]) : otherRequest(path));
  }));
  const view = page();
  fireEvent.click(view.container.querySelector(".btn-connector-add")!);
  fireEvent.click(await screen.findByTitle("Remove connector"));
  expect(await within(screen.getByRole("dialog")).findByRole("alert")).toHaveTextContent("Could not remove connector definition: HTTP 403");
  expect(screen.getByText("old account")).toBeInTheDocument();
});

it("a disconnect receipt still updates the agent form and summary after the initiating page unmounts", async () => {
  const deletion = deferred();
  let removed = false;
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    if (init?.method === "DELETE") return deletion.promise;
    return Promise.resolve(path === "/api/connectors" ? Response.json(removed ? [] : [installed()]) : otherRequest(path));
  }));
  const original = page();
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Disconnect old account" }));
  original.unmount();
  useSessionStore.setState({ pageTab: "form" });
  const consumer = render(<><AgentFormPage onToggleSidebar={() => {}} /><SidebarManage /></>);
  expect(await screen.findByRole("checkbox", { name: /old account/ })).toBeInTheDocument();
  removed = true;
  await act(async () => deletion.resolve(new Response(null, { status: 204 })));
  expect(screen.queryByRole("checkbox", { name: /old account/ })).toBeNull();
  expect(consumer.container.querySelector(".btn-manage-connectors .manage-summary")).toHaveTextContent(/^0$/);
});

it("a pending connection list observation survives leaving its page", async () => {
  const observation = deferred();
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    const path = new URL(url).pathname;
    return path === "/api/connectors" ? observation.promise : Promise.resolve(otherRequest(path));
  }));
  const original = page();
  original.unmount();
  useSessionStore.setState({ pageTab: "form" });
  render(<AgentFormPage onToggleSidebar={() => {}} />);
  await act(async () => observation.resolve(Response.json([installed("new")])));
  expect(screen.getByRole("checkbox", { name: /new account/ })).toBeInTheDocument();
});

it("expires a stalled response body without retrying, ignores its late result, and allows a fresh read", async () => {
  const body = deferred<ConnectorInstallationInfo[]>();
  const response = Response.json([]);
  vi.spyOn(response, "json").mockReturnValue(body.promise);
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    const path = new URL(url).pathname;
    if (path === "/api/connectors") return Promise.resolve(++reads === 1 ? response : Response.json([installed("new")]));
    return Promise.resolve(otherRequest(path));
  }));
  vi.useFakeTimers();
  page();
  await act(async () => {});
  await act(async () => vi.advanceTimersByTimeAsync(30_000));
  expect(screen.getByRole("alert")).toHaveTextContent("timed out after 30 seconds");
  expect(screen.getByText("old account")).toBeVisible();
  expect(reads).toBe(1);
  await act(async () => body.resolve([installed("late")]));
  expect(screen.queryByText("late account")).toBeNull();
  vi.useRealTimers();
  fireEvent.click(screen.getByRole("button", { name: "Refresh connections" }));
  expect(await screen.findByText("new account")).toBeVisible();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("an uncontested connection uses a post-login GET instead of the pending pre-login list", async () => {
  const old = deferred();
  let reads = 0;
  vi.spyOn(window, "open").mockReturnValue(null);
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    const path = new URL(url).pathname;
    if (path === "/api/connectors/oauth/start") return Promise.resolve(Response.json({ login_id: "login", authorize_url: "https://fake.test/auth" }));
    if (path.includes("/oauth/status/")) return Promise.resolve(Response.json({ status: "success", installation_id: "new" }));
    if (path === "/api/connectors") return ++reads === 1 ? old.promise : Promise.resolve(Response.json([installed("new")]));
    return Promise.resolve(otherRequest(path));
  }));
  const view = page();
  fireEvent.click(view.container.querySelector(".btn-connector-add")!);
  fireEvent.click(await screen.findByRole("button", { name: "Connect" }));
  expect(await screen.findByText("new account", {}, { timeout: 2500 })).toBeVisible();
  expect(screen.queryByRole("dialog")).toBeNull();
  await act(async () => old.resolve(Response.json([installed()])));
  expect(screen.queryByText("old account")).toBeNull();
  expect(screen.getByText("new account")).toBeVisible();
  expect(reads).toBe(2);
});
