import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSessionStore } from "../stores/sessionStore";
import { ConnectorsPage } from "./ConnectorsPage";
import { SidebarManage } from "./SidebarManage";
import { deferred } from "../test-support/deferred";

const startReply = (id = "original-login") => Response.json({
  login_id: id, authorize_url: `https://fake.test/auth/${id}`,
});
function background(path: string) {
  return Response.json(path === "/api/connectors/catalog"
    ? [{ kind: "fake", display_name: "Fake", available: true }] : []);
}
async function begin() {
  fireEvent.click(screen.getByRole("button", { name: "+ New Connector" }));
  fireEvent.click(await screen.findByRole("button", { name: "Connect" }));
  await screen.findByText("Starting sign-in…");
}
beforeEach(() => {
  useSessionStore.setState({
    ...useSessionStore.getInitialState(), token: "original-account",
    accountGeneration: useSessionStore.getState().accountGeneration + 1, dispatchGeneration: "service-a",
  });
  vi.spyOn(window, "open").mockReturnValue(null);
});
afterEach(() => {
  cleanup(); useSessionStore.getState().setToken("");
  vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks();
});

it.each(["first", "second", "new"])("sends the exact %s connection target from the rendered button", async (selected) => {
  const requests: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    if (path === "/api/connectors") return Promise.resolve(Response.json(["first", "second"].map((id) => ({
      id, kind: "fake", label: `${id} connection`, needs_reconnect: true, scopes: [],
    }))));
    if (path.endsWith("/oauth/start")) {
      requests.push(JSON.parse(init!.body as string));
      return Promise.resolve(startReply());
    }
    if (path.endsWith("/oauth/cancel")) return Promise.resolve(Response.json({ status: "cancelled" }));
    return Promise.resolve(background(path));
  }));
  render(<ConnectorsPage onToggleSidebar={() => {}} />);
  await screen.findByText("first connection");
  await screen.findByText("second connection");
  if (selected === "new") {
    fireEvent.click(screen.getByRole("button", { name: "+ New Connector" }));
    fireEvent.click(await screen.findByRole("button", { name: "Connect" }));
  } else {
    const card = screen.getByText(`${selected} connection`).closest(".connector-item")!;
    fireEvent.click(within(card as HTMLElement).getByRole("button", { name: "Reconnect →" }));
  }
  await screen.findByText("Waiting for sign-in in the opened tab…");
  expect(requests).toEqual([{
    kind: "fake", label: null, ...(selected === "new" ? {} : { installation_id: selected }),
  }]);
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});

it("closing during start consumes the late ID without opening a tab or resuming UI polling", async () => {
  const start = deferred<Response>();
  const requests: string[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    const path = new URL(url).pathname; requests.push(path);
    if (path.endsWith("/oauth/start")) return start.promise;
    if (path.endsWith("/oauth/cancel")) {
      expect(JSON.parse(init!.body as string)).toEqual({ login_id: "original-login" });
      expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer original-account");
      return Promise.resolve(Response.json({ status: "cancelled" }));
    }
    return Promise.resolve(background(path));
  }));
  render(<ConnectorsPage onToggleSidebar={() => {}} />);
  await begin();
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  await act(async () => start.resolve(startReply()));
  await waitFor(() => expect(requests).toContain("/api/connectors/oauth/cancel"));
  expect(window.open).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(requests.filter((path) => path.includes("/oauth/status/"))).toHaveLength(0);
});

it("closing while a status response is stalled still cancels the original login", async () => {
  const start = deferred<Response>();
  const status = deferred<Response>();
  let polls = 0;
  let cancels = 0;
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/oauth/start")) return start.promise;
    if (path.includes("/oauth/status/")) { polls++; return status.promise; }
    if (path.endsWith("/oauth/cancel")) { cancels++; return Promise.resolve(Response.json({ status: "cancelled" })); }
    return Promise.resolve(background(path));
  }));
  render(<ConnectorsPage onToggleSidebar={() => {}} />);
  await begin();
  vi.useFakeTimers();
  await act(async () => start.resolve(startReply()));
  await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
  expect(polls).toBe(1);
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  await act(async () => {});
  expect(cancels).toBe(1);
  expect(screen.queryByRole("dialog")).toBeNull();
  await act(async () => status.resolve(Response.json({ status: "pending" })));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(polls).toBe(1);
});

it.each(["unmount", "account ABA", "service ABA"])("%s permanently retires UI rights while the original token consumes a late login ID", async (retirement) => {
  const start = deferred<Response>();
  const cancellations: string[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/oauth/start")) return start.promise;
    if (path.endsWith("/oauth/cancel")) {
      cancellations.push(new Headers(init?.headers).get("Authorization")!);
      return Promise.resolve(Response.json({ status: "cancelled" }));
    }
    return Promise.resolve(background(path));
  }));
  const view = render(<ConnectorsPage onToggleSidebar={() => {}} />);
  await begin();
  if (retirement === "unmount") view.unmount();
  else await act(async () => {
    if (retirement === "account ABA") {
      useSessionStore.getState().setToken("other-account");
      useSessionStore.getState().setToken("original-account");
    } else {
      useSessionStore.setState({ dispatchGeneration: "service-b" });
      useSessionStore.setState({ dispatchGeneration: "service-a" });
    }
  });
  expect(screen.queryByRole("dialog")).toBeNull();
  await act(async () => start.resolve(startReply()));
  await waitFor(() => expect(cancellations).toEqual(["Bearer original-account"]));
  expect(window.open).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("a lost cancel receipt reports unknown and checks for actual installation changes without replaying the write", async () => {
  let saved = false;
  let cancels = 0;
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/oauth/start")) return Promise.resolve(startReply());
    if (path.endsWith("/oauth/cancel")) {
      saved = true; cancels++; return Promise.reject(new TypeError("Connection lost"));
    }
    if (path === "/api/connectors") return Promise.resolve(Response.json(saved
      ? [{ id: "saved", kind: "fake", label: "Saved account", needs_reconnect: false, scopes: [] }] : []));
    return Promise.resolve(background(path));
  }));
  render(<ConnectorsPage onToggleSidebar={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "+ New Connector" }));
  fireEvent.click(await screen.findByRole("button", { name: "Connect" }));
  await screen.findByText("Waiting for sign-in in the opened tab…");
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(await screen.findByText(/Connection outcome is unknown/)).toHaveTextContent("Connection lost");
  await waitFor(() => expect(screen.getByText("Saved account")).toBeInTheDocument());
  expect(screen.getByRole("dialog")).toBeVisible();
  expect(cancels).toBe(1);
});

it("a polled cancellation preserves the visible sign-in explanation", async () => {
  const start = deferred<Response>();
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/oauth/start")) return start.promise;
    if (path.includes("/oauth/status/")) return Promise.resolve(Response.json({ status: "cancelled", message: "Local connection cancelled; remote authorization is unchanged" }));
    return Promise.resolve(background(path));
  }));
  render(<ConnectorsPage onToggleSidebar={() => {}} />);
  await begin();
  vi.useFakeTimers();
  await act(async () => start.resolve(startReply()));
  await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
  expect(screen.getByRole("dialog")).toHaveTextContent("Local connection cancelled; remote authorization is unchanged");
});

it.each(["success", "error"])("a retired %s receipt checks installations without closing a newer login", async (status) => {
  const oldStart = deferred<Response>();
  const newStart = deferred<Response>();
  const oldCancel = deferred<Response>();
  let starts = 0;
  let saved = false;
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/oauth/start")) return ++starts === 1 ? oldStart.promise : newStart.promise;
    if (path.endsWith("/oauth/cancel")) return oldCancel.promise;
    if (path === "/api/connectors") return Promise.resolve(Response.json(saved
      ? [{ id: "saved", kind: "fake", label: "Saved account", scopes: [] }] : []));
    return Promise.resolve(background(path));
  }));
  render(<ConnectorsPage onToggleSidebar={() => {}} />);
  await begin();
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  await begin();
  await act(async () => oldStart.resolve(startReply("old-login")));
  expect(window.open).not.toHaveBeenCalled();
  await act(async () => newStart.resolve(startReply("new-login")));
  expect(window.open).toHaveBeenCalledExactlyOnceWith("https://fake.test/auth/new-login", "_blank", "noopener,noreferrer");
  saved = true;
  await act(async () => oldCancel.resolve(Response.json({ status, message: "Local connection saving is unconfirmed" })));
  await waitFor(() => expect(screen.getByText("Saved account")).toBeInTheDocument());
  expect(screen.getByRole("dialog")).toHaveTextContent("Waiting for sign-in in the opened tab…");
  expect(screen.queryByText("Local connection saving is unconfirmed")).toBeNull();
});

it.each(["same account", "different account"])("a success after unmount only reconciles its %s", async (scope) => {
  const start = deferred<Response>();
  let saved = false;
  const listTokens: string[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/oauth/start")) return start.promise;
    if (path.endsWith("/oauth/cancel")) {
      saved = true; return Promise.resolve(Response.json({ status: "success", installation_id: "saved" }));
    }
    if (path === "/api/connectors") {
      listTokens.push(new Headers(init?.headers).get("Authorization")!);
      return Promise.resolve(Response.json(saved ? [{ id: "saved", kind: "fake", label: "Saved account", scopes: [] }] : []));
    }
    return Promise.resolve(background(path));
  }));
  const view = render(<ConnectorsPage onToggleSidebar={() => {}} />);
  await begin();
  view.unmount();
  if (scope === "different account") useSessionStore.getState().setToken("different-account");
  const sidebar = render(<SidebarManage />);
  await act(async () => start.resolve(startReply()));
  expect(window.open).not.toHaveBeenCalled();
  await waitFor(() => expect(sidebar.container.querySelector(".btn-manage-connectors .manage-summary"))
    .toHaveTextContent(scope === "same account" ? /^1$/ : /^0$/));
  expect(listTokens).toHaveLength(scope === "same account" ? 2 : 1);
});

it.each([401, 404])("HTTP %s from cancel is an unknown outcome rather than cancellation success", async (status) => {
  let cancels = 0;
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/oauth/start")) return Promise.resolve(startReply());
    if (path.endsWith("/oauth/cancel")) {
      cancels++; return Promise.resolve(Response.json({ detail: `HTTP ${status}` }, { status }));
    }
    return Promise.resolve(background(path));
  }));
  render(<ConnectorsPage onToggleSidebar={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "+ New Connector" }));
  fireEvent.click(await screen.findByRole("button", { name: "Connect" }));
  await screen.findByText("Waiting for sign-in in the opened tab…");
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(await screen.findByText(/Connection outcome is unknown/)).toHaveTextContent(`HTTP ${status}`);
  expect(screen.getByRole("dialog")).toBeVisible();
  expect(cancels).toBe(1);
});

it("a start body deadline reports unknown without replaying start or opening a late tab", async () => {
  const body = deferred<{ login_id: string; authorize_url: string }>();
  const response = Response.json({});
  vi.spyOn(response, "json").mockImplementation(() => body.promise);
  let starts = 0;
  let cancels = 0;
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/oauth/start")) { starts++; return Promise.resolve(response); }
    if (path.endsWith("/oauth/cancel")) cancels++;
    return Promise.resolve(background(path));
  }));
  render(<ConnectorsPage onToggleSidebar={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "+ New Connector" }));
  const connect = await screen.findByRole("button", { name: "Connect" });
  vi.useFakeTimers();
  fireEvent.click(connect);
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(screen.getByRole("dialog")).toHaveTextContent("Connection outcome is unknown: OAuth response timed out");
  await act(async () => body.resolve({ login_id: "lost-login", authorize_url: "https://fake.test/late" }));
  expect(window.open).not.toHaveBeenCalled();
  expect(starts).toBe(1);
  expect(cancels).toBe(0);
});

it("a cancel deadline keeps its unknown result visible without automatically sending another cancellation", async () => {
  const start = deferred<Response>();
  const cancel = deferred<Response>();
  let cancels = 0;
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/oauth/start")) return start.promise;
    if (path.endsWith("/oauth/cancel")) { cancels++; return cancel.promise; }
    return Promise.resolve(background(path));
  }));
  render(<ConnectorsPage onToggleSidebar={() => {}} />);
  await begin();
  vi.useFakeTimers();
  await act(async () => start.resolve(startReply()));
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(screen.getByRole("dialog")).toHaveTextContent("Confirming connection cancellation…");
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(screen.getByRole("dialog")).toHaveTextContent("Connection outcome is unknown");
  expect(cancels).toBe(1);
  await act(async () => cancel.resolve(Response.json({ status: "cancelled" })));
  expect(screen.getByRole("dialog")).toHaveTextContent("Connection outcome is unknown");
});

it("the polling deadline consumes an authoritative cancellation and retains the timeout explanation", async () => {
  const start = deferred<Response>();
  let cancels = 0;
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/oauth/start")) return start.promise;
    if (path.includes("/oauth/status/")) return Promise.resolve(Response.json({ status: "processing" }));
    if (path.endsWith("/oauth/cancel")) { cancels++; return Promise.resolve(Response.json({ status: "cancelled" })); }
    return Promise.resolve(background(path));
  }));
  render(<ConnectorsPage onToggleSidebar={() => {}} />);
  await begin();
  vi.useFakeTimers();
  await act(async () => start.resolve(startReply()));
  await act(async () => { await vi.advanceTimersByTimeAsync(5 * 60_000); });
  expect(screen.getByRole("dialog")).toHaveTextContent("Timed out waiting for sign-in. Local connection cancelled");
  expect(cancels).toBe(1);
});
