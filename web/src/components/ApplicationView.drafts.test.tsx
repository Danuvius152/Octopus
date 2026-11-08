import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ApplicationView } from "./ApplicationView";
import { applications } from "../lib/applicationList";
import { useSessionStore, type Application } from "../stores/sessionStore";
import { deferred } from "../test-support/deferred";

const app = (id: string, patch: Partial<Application> = {}): Application => ({
  id, name: `App ${id}`, description: "", icon: "", agent_id: "agent", session_id: null,
  app_dir: `/tmp/${id}`, entrypoint: "index.html", status: "ready", error: null,
  archived: false, pinned: true, pin_order: 1, created_at: "2026-01-01", updated_at: "2026-01-01", last_built_at: null,
  ...patch,
});
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { "Content-Type": "application/json" },
});
let rows: Application[];
let transport: ReturnType<typeof vi.fn>;
const box = () => screen.getByRole("textbox") as HTMLTextAreaElement;
function open() { fireEvent.click(document.querySelector(".btn-application-iterate")!); }
function select(id: string) { act(() => useSessionStore.setState({ activeApplicationId: id })); }
function edit(value: string) { fireEvent.change(box(), { target: { value } }); }
function send() { fireEvent.click(screen.getByLabelText("Send the change request")); }

beforeEach(() => {
  sessionStorage.clear();
  useSessionStore.getState().setToken("draft-token");
  useSessionStore.getState().setIdentity({ user_id: "draft-account", label: "Draft account", is_admin: false });
  rows = [app("a"), app("b")];
  useSessionStore.setState({ applications: rows, activeApplicationId: "a", mainView: "application" });
  transport = vi.fn(async () => json(rows));
  vi.stubGlobal("fetch", transport);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  useSessionStore.getState().setToken("");
  vi.unstubAllGlobals();
});

it("keeps B's draft and dialog when A's build is acknowledged, and still adopts A's result", async () => {
  const build = deferred<Response>();
  transport.mockImplementation((url: unknown) => String(url).endsWith("/a/build") ? build.promise : Promise.resolve(json(rows)));
  render(<ApplicationView onToggleSidebar={() => {}} />);
  open(); edit("change A"); send();
  select("b");
  if (!screen.queryByRole("textbox")) open();
  edit("new B draft");
  expect(screen.getByLabelText("Send the change request")).not.toBeDisabled();
  rows = [app("a", { status: "building" }), app("b")];
  await act(async () => build.resolve(json(rows[0])));
  await waitFor(() => expect(box().value).toBe("new B draft"));
  expect(screen.getByLabelText("Send the change request")).not.toBeDisabled();
  expect(useSessionStore.getState().applications.find(row => row.id === "a")?.status).toBe("building");
  select("a"); open();
  expect(box().value).toBe("");
});

it("restores only after the stable account is confirmed, without submitting", () => {
  useSessionStore.getState().setIdentity(null);
  sessionStorage.setItem('octopus.text-draft.v1:["draft-account","application","a"]', "saved before refresh");
  render(<ApplicationView onToggleSidebar={() => {}} />);
  open();
  expect(box().value).toBe("");
  act(() => useSessionStore.getState().setIdentity({ user_id: "draft-account", label: "Draft account", is_admin: false }));
  expect(box().value).toBe("saved before refresh");
  expect(transport).not.toHaveBeenCalled();
});

it("removes the submitted version from refresh recovery immediately and keeps a newer A draft on acknowledgment", async () => {
  const build = deferred<Response>();
  transport.mockImplementation((url: unknown) => String(url).endsWith("/a/build") ? build.promise : Promise.resolve(json(rows)));
  render(<ApplicationView onToggleSidebar={() => {}} />);
  open(); edit("submitted A");
  const key = 'octopus.text-draft.v1:["draft-account","application","a"]';
  expect(sessionStorage.getItem(key)).toBe("submitted A");
  send();
  expect(box().value).toBe("submitted A");
  expect(sessionStorage.getItem(key)).toBeNull();
  edit("newer A");
  await act(async () => build.resolve(json(app("a"))));
  expect(box().value).toBe("newer A");
  expect(sessionStorage.getItem(key)).toBe("newer A");
  expect(screen.getByLabelText("Send the change request")).not.toBeDisabled();
});

it("clears a confirmed deletion despite a competing list change and retires the old submission record", async () => {
  const build = deferred<Response>(), deletion = deferred<Response>(), list = deferred<Response>();
  transport.mockImplementation((url: unknown, init?: RequestInit) => {
    if (init?.method === "DELETE") return deletion.promise;
    return String(url).endsWith("/a/build") ? build.promise : list.promise;
  });
  const view = render(<ApplicationView onToggleSidebar={() => {}} />);
  open(); edit("old A"); send(); edit("edited A");
  let removed!: Promise<void>;
  act(() => { removed = applications.remove("a", true); });
  act(() => applications.receive({ type: "application_updated", application: app("b", { name: "Changed B" }) }));
  await act(async () => { deletion.resolve(new Response(null, { status: 204 })); await removed; });
  expect(sessionStorage.getItem('octopus.text-draft.v1:["draft-account","application","a"]')).toBeNull();
  view.unmount();
  render(<ApplicationView onToggleSidebar={() => {}} />); open(); edit("fresh record");
  await act(async () => build.resolve(json({ detail: "old build failed" }, 503)));
  expect(box().value).toBe("fresh record");
  expect(screen.queryByText(/old build failed/)).toBeNull();
  expect(screen.getByLabelText("Send the change request")).not.toBeDisabled();
});

it.each(["switch", "reopen"])("does not close a new A dialog after %s, while clearing only the acknowledged version", async mode => {
  const build = deferred<Response>();
  transport.mockImplementation((url: unknown) => String(url).endsWith("/a/build") ? build.promise : Promise.resolve(json(rows)));
  render(<ApplicationView onToggleSidebar={() => {}} />);
  open(); edit("original A"); send();
  if (mode === "switch") { select("b"); select("a"); }
  else fireEvent.keyDown(document, { key: "Escape" });
  open();
  expect(box().value).toBe("original A");
  expect(screen.getByLabelText("Send the change request")).toBeDisabled();
  await act(async () => build.resolve(json(app("a"))));
  expect(box().value).toBe("");
  expect(screen.getByRole("dialog")).toBeVisible();
});

it("retains a failed A request and error only on A, including after unmount", async () => {
  const build = deferred<Response>();
  transport.mockImplementation((url: unknown) => String(url).endsWith("/a/build") ? build.promise : Promise.resolve(json(rows)));
  const view = render(<ApplicationView onToggleSidebar={() => {}} />);
  open(); edit("unconfirmed A"); send();
  select("b"); open(); edit("B is independent");
  view.unmount();
  await act(async () => build.reject(new TypeError("connection lost")));
  render(<ApplicationView onToggleSidebar={() => {}} />); open();
  expect(box().value).toBe("B is independent");
  expect(screen.queryByText(/Outcome unknown/)).toBeNull();
  select("a"); open();
  expect(box().value).toBe("unconfirmed A");
  expect(screen.getByText(/Outcome unknown/)).toBeVisible();
  expect(screen.getByLabelText("Send the change request")).not.toBeDisabled();
});

it("lets input before identity confirmation win over saved text and synchronously saves edits and clearing", () => {
  useSessionStore.getState().setIdentity(null);
  const key = 'octopus.text-draft.v1:["draft-account","application","a"]';
  sessionStorage.setItem(key, "old saved A");
  render(<ApplicationView onToggleSidebar={() => {}} />); open(); edit("typed before identity");
  act(() => useSessionStore.getState().setIdentity({ user_id: "draft-account", label: "Draft account", is_admin: false }));
  expect(box().value).toBe("typed before identity");
  expect(sessionStorage.getItem(key)).toBe("typed before identity");
  edit("");
  expect(sessionStorage.getItem(key)).toBeNull();
});

it.each(["logout", "different identity"])("clears saved entries for unopened Apps on %s, without account ABA revival", mode => {
  sessionStorage.setItem('octopus.text-draft.v1:["draft-account","application","unopened"]', "private unopened");
  render(<ApplicationView onToggleSidebar={() => {}} />); open(); edit("private A");
  act(() => {
    if (mode === "logout") useSessionStore.getState().setToken("");
    else useSessionStore.getState().setIdentity({ user_id: "other-account", label: "Other", is_admin: false });
  });
  expect(Object.keys(sessionStorage).filter(key => key.startsWith("octopus.text-draft.v1:"))).toEqual([]);
  act(() => {
    useSessionStore.getState().setToken("draft-token");
    useSessionStore.getState().setIdentity({ user_id: "draft-account", label: "Draft account", is_admin: false });
    useSessionStore.setState({ applications: rows, activeApplicationId: "a" });
  });
  open(); expect(box().value).toBe("");
});

it("keeps editing and sending available when sessionStorage rejects writes", async () => {
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("denied", "SecurityError"); });
  render(<ApplicationView onToggleSidebar={() => {}} />); open(); edit("keep this text");
  expect(box().value).toBe("keep this text");
  expect(screen.getByText(/Text draft could not be saved/)).toBeVisible();
  send();
  await waitFor(() => expect(transport.mock.calls.some(([url]) => String(url).endsWith("/a/build"))).toBe(true));
});

it.each(["archive", "delete"])("keeps a late %s error and busy state on the original App", async action => {
  const response = deferred<Response>();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  if (action === "delete") {
    rows[0] = app("a", { deletion: { keep_files: true, error: "unfinished", phase: "files" } });
    useSessionStore.setState({ applications: rows });
  }
  transport.mockImplementation((_url: unknown, init?: RequestInit) => init?.method ? response.promise : Promise.resolve(json(rows)));
  render(<ApplicationView onToggleSidebar={() => {}} />);
  fireEvent.click(action === "archive" ? screen.getByLabelText("Archive this application") : screen.getByText("Retry deletion"));
  select("b"); open(); edit("B while A waits");
  expect(screen.getByLabelText("Send the change request")).not.toBeDisabled();
  await act(async () => response.resolve(json({ detail: `A ${action} denied` }, 403)));
  expect(screen.queryByText(`A ${action} denied`)).toBeNull();
  select("a");
  expect(screen.getByText(`A ${action} denied`)).toBeVisible();
});

it("clears event-confirmed deletion of an unopened App without treating archive as deletion", () => {
  const key = 'octopus.text-draft.v1:["draft-account","application","b"]';
  sessionStorage.setItem(key, "unopened B");
  render(<ApplicationView onToggleSidebar={() => {}} />);
  act(() => applications.receive({ type: "application_archived", application: app("b", { archived: true }) }));
  expect(sessionStorage.getItem(key)).toBe("unopened B");
  act(() => applications.receive({ type: "application_deleted", application_id: "b" }));
  expect(sessionStorage.getItem(key)).toBeNull();
});

it("does not let an old account's acknowledgment clear a new account's draft after ABA", async () => {
  const build = deferred<Response>();
  transport.mockImplementation((url: unknown) => String(url).endsWith("/a/build") ? build.promise : Promise.resolve(json(rows)));
  render(<ApplicationView onToggleSidebar={() => {}} />); open(); edit("old account"); send();
  act(() => {
    useSessionStore.getState().setToken("other-token");
    useSessionStore.getState().setIdentity({ user_id: "other", label: "Other", is_admin: false });
    useSessionStore.getState().setToken("draft-token");
    useSessionStore.getState().setIdentity({ user_id: "draft-account", label: "Draft account", is_admin: false });
    useSessionStore.setState({ applications: rows, activeApplicationId: "a" });
  });
  open(); edit("new login draft");
  await act(async () => build.resolve(json(app("a", { status: "building" }))));
  expect(box().value).toBe("new login draft");
  expect(screen.queryByText(/earlier connection/)).toBeNull();
  expect(screen.getByLabelText("Send the change request")).not.toBeDisabled();
});

it("clears the acknowledged version across a service change while the list confirms the real result", async () => {
  const build = deferred<Response>();
  transport.mockImplementation((url: unknown) => String(url).endsWith("/a/build") ? build.promise : Promise.resolve(json(rows)));
  render(<ApplicationView onToggleSidebar={() => {}} />); open(); edit("A before reconnect"); send();
  act(() => useSessionStore.setState({ dispatchGeneration: "new-service" }));
  rows = [app("a", { status: "building" }), app("b")];
  await act(async () => build.resolve(json(rows[0])));
  expect(screen.queryByRole("textbox")).toBeNull();
  open(); expect(box().value).toBe("");
  await waitFor(() => expect(useSessionStore.getState().applications[0].status).toBe("building"));
});

it("reports denied reads and retains typed text without claiming it was saved", () => {
  const getItem = Storage.prototype.getItem;
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(function (this: Storage, key) {
    if (key.startsWith("octopus.text-draft.v1:")) throw new DOMException("denied", "SecurityError");
    return getItem.call(this, key);
  });
  render(<ApplicationView onToggleSidebar={() => {}} />); open();
  expect(screen.getByText(/Text draft could not be saved/)).toBeVisible();
  edit("still editable");
  expect(box().value).toBe("still editable");
});

it("clears a saved unopened App on confirmed deletion even before identity arrives", async () => {
  useSessionStore.getState().setIdentity(null);
  const key = 'octopus.text-draft.v1:["draft-account","application","b"]';
  sessionStorage.setItem(key, "saved B before reload");
  render(<ApplicationView onToggleSidebar={() => {}} />);
  await act(async () => applications.receive({ type: "application_deleted", application_id: "b" }));
  expect(sessionStorage.getItem(key)).toBeNull();
});
