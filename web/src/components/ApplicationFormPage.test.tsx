/**
 * The Applications page — the Create form, and the All tab (sidebar-pins.md
 * §6): every application by section, with open, pin, archive, restore and
 * delete.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ApplicationFormPage } from "./ApplicationFormPage";
import { applications as applicationList } from "../lib/applicationList";
import { useSessionStore, type Agent, type Application } from "../stores/sessionStore";

const agent = { id: "ag1", name: "Octo", avatar: "🐙", is_system: true, backend: "claude-code" } as unknown as Agent;

function application(o: Partial<Application> = {}): Application {
  return {
    id: "a1",
    name: "Habit Tracker",
    description: "Track habits",
    icon: "✅",
    agent_id: "ag1",
    session_id: "s1",
    app_dir: "/tmp/apps/habit",
    entrypoint: "index.html",
    status: "ready",
    error: null,
    archived: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    last_built_at: null,
    pinned: true,
    pin_order: 1,
    ...o,
  } as Application;
}

let fetchMock: ReturnType<typeof vi.fn>;

function mount(
  pageTab: "all" | "form" = "form",
  applications: Application[] = []
) {
  useSessionStore.setState({
    token: "tok",
    agents: [agent],
    activeAgentId: "ag1",
    applications,
    mainView: "application-create",
    pageTab,
  });
  return render(<ApplicationFormPage onToggleSidebar={() => {}} />);
}

beforeEach(() => {
  useSessionStore.setState({ token: "tok", accountGeneration: useSessionStore.getState().accountGeneration + 1 });
  fetchMock = vi.fn(async () => new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  useSessionStore.getState().setToken("");
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ApplicationFormPage", () => {
  it("freezes every creation field through POST and pending list confirmation without creating twice", async () => {
    let finishWrite!: (response: Response) => void;
    let synchronized = false;
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return new Promise<Response>(resolve => { finishWrite = resolve; });
      return synchronized ? json([application({ archived: false })]) : json({ detail: "offline" }, 503);
    });
    const { container } = mount();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Frozen snapshot" } });
    fireEvent.change(screen.getByLabelText(/What problem/), { target: { value: "Original brief" } });
    const save = screen.getByRole("button", { name: /Start Build/ });
    act(() => { save.click(); save.click(); });
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
    const expectFrozen = () => {
      for (const control of container.querySelectorAll(".page-body input, .page-body textarea, .page-body select, .page-body .app-agent-option")) {
        expect(control).toBeDisabled();
      }
      expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
    };
    expectFrozen();
    await act(async () => {
      applicationList.receive({ type: "application_updated", application: application({ id: "other", archived: false }) });
      finishWrite(json(application({ archived: false }), 201));
    });
    await screen.findByText(/List not synchronized/);
    expectFrozen();
    synchronized = true;
    fireEvent.click(screen.getByRole("button", { name: /Refresh applications/ }));
    await waitFor(() => expect(useSessionStore.getState().activeApplicationId).toBe("a1"));
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });

  it("allows tab navigation while preventing new All-tab configuration writes during creation", async () => {
    let finish!: (response: Response) => void;
    const live = application({ archived: false });
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => init?.method === "POST"
      ? new Promise<Response>(resolve => { finish = resolve; })
      : json(String(url).includes("archived=true") ? [] : [live]));
    mount("form", [live]);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Pending" } });
    fireEvent.change(screen.getByLabelText(/What problem/), { target: { value: "Brief" } });
    fireEvent.click(screen.getByRole("button", { name: /Start Build/ }));
    fireEvent.click(screen.getByRole("button", { name: /^All/ }));
    expect(screen.getByLabelText("Archive Habit Tracker")).toBeDisabled();
    expect(screen.getByLabelText("Unpin Habit Tracker from the sidebar")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Open" })).toBeEnabled();
    await act(async () => finish(json({ detail: "Creation refused" }, 409)));
    expect(screen.getByLabelText("Archive Habit Tracker")).toBeEnabled();
  });

  it("keeps Start Build disabled until there's a name and a brief", () => {
    mount();
    const submit = screen.getByRole("button", { name: /Start Build/ });
    expect((submit as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Tracker" } });
    expect((submit as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(/What problem/), {
      target: { value: "track things" },
    });
    expect((submit as HTMLButtonElement).disabled).toBe(false);
  });

  it("POSTs the brief and opens the new application", async () => {
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) =>
      String(url).endsWith("/api/applications") && init?.method === "POST"
        ? new Response(JSON.stringify(application({ archived: false })), {
            status: 201,
            headers: { "Content-Type": "application/json" },
          })
        : json([application({ archived: false })])
    );
    mount();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Habit Tracker" } });
    fireEvent.change(screen.getByLabelText(/What problem/), {
      target: { value: "track habits" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Start Build/ }));

    await waitFor(() =>
      expect(useSessionStore.getState().mainView).toBe("application")
    );
    expect(useSessionStore.getState().activeApplicationId).toBe("a1");
    const body = fetchMock.mock.calls.find(
      ([u, i]) => String(u).endsWith("/api/applications") && (i as RequestInit)?.method === "POST"
    )?.[1] as RequestInit;
    expect(JSON.parse(String(body.body))).toMatchObject({
      name: "Habit Tracker",
      description: "track habits",
      agent_id: "ag1",
    });
  });

  it("lists every application on the All tab, by section", async () => {
    const live = [
      application({ id: "a1", archived: false, pin_order: 2 }),
      application({ id: "a2", name: "Reader", archived: false, pin_order: 1 }),
      application({ id: "a3", name: "Scratch", archived: false, pinned: false }),
    ];
    fetchMock.mockImplementation(async (url: unknown) =>
      String(url).includes("archived=true")
        ? json([application({ id: "a9", name: "Old Report" })])
        : json(live)
    );
    const { container } = mount("all", live);
    await waitFor(() => expect(screen.getByText("Old Report")).toBeTruthy());
    const names = (section: string) =>
      [...container.querySelectorAll(`.library-${section} .library-name`)].map(
        (n) => n.textContent
      );
    // "In sidebar" follows the sidebar's order, not creation order.
    expect(names("pinned")).toEqual(["Reader", "Habit Tracker"]);
    expect(names("unpinned")).toEqual(["Scratch"]);
    expect(names("archived")).toEqual(["Old Report"]);
    expect(screen.getByText("All 4")).toBeTruthy();
  });

  it("opens, pins and unpins from a row", async () => {
    const scratch = application({ id: "a3", name: "Scratch", archived: false, pinned: false });
    let row = scratch;
    fetchMock.mockImplementation(async (url: unknown) => {
      if (String(url).endsWith("/a3/pin")) { row = { ...row, pinned: true, pin_order: 2 }; return json(row); }
      return json(String(url).includes("archived=true") ? [] : [row]);
    });
    mount("all", [scratch]);
    fireEvent.click(screen.getByLabelText("Pin Scratch to the sidebar"));
    await waitFor(() =>
      expect(useSessionStore.getState().applications[0].pinned).toBe(true)
    );
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(useSessionStore.getState().mainView).toBe("application");
    expect(useSessionStore.getState().activeApplicationId).toBe("a3");
  });

  it("archives a row into the Archived section", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const live = application({ archived: false });
    let row = live;
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (String(url).endsWith("/a1/archive") && init?.method === "POST") {
        row = { ...row, archived: true }; return json(row);
      }
      return json(row.archived === String(url).includes("archived=true") ? [row] : []);
    });
    const { container } = mount("all", [live]);
    fireEvent.click(screen.getByLabelText("Archive Habit Tracker"));
    await waitFor(() =>
      expect(
        container.querySelector(".library-archived .library-name")?.textContent
      ).toBe("Habit Tracker")
    );
    expect(useSessionStore.getState().applications).toEqual([]);
  });

  it("never offers Delete — archive is the only way out", async () => {
    fetchMock.mockImplementation(async (url: unknown) =>
      String(url).includes("archived=true")
        ? json([application({ id: "a9", name: "Old Report" })])
        : json([application({ archived: false })])
    );
    mount("all", [application({ archived: false })]);
    await waitFor(() => expect(screen.getByText("Old Report")).toBeTruthy());
    expect(screen.queryByLabelText(/^Delete /)).toBeNull();
    expect(screen.getByLabelText("Archive Habit Tracker")).toBeTruthy();
  });

  it("restore puts it back pinned and stays on the page", async () => {
    let restored = false;
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (String(url).includes("/unarchive") && init?.method === "POST") {
        restored = true;
        return json(application({ archived: false }));
      }
      if (String(url).includes("archived=true")) return json(restored ? [] : [application()]);
      return json(restored ? [application({ archived: false })] : []);
    });
    const { container } = mount("all");
    await waitFor(() => expect(screen.getByText("Habit Tracker")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));

    await waitFor(() =>
      expect(useSessionStore.getState().applications.map((a) => a.id)).toEqual(["a1"])
    );
    expect(useSessionStore.getState().mainView).toBe("application-create");
    await waitFor(() =>
      expect(
        container.querySelector(".library-pinned .library-name")?.textContent
      ).toBe("Habit Tracker")
    );
  });

  it("says so plainly when there are none", async () => {
    mount("all");
    await waitFor(() => expect(screen.getByText(/No applications yet/)).toBeTruthy());
    fireEvent.click(screen.getByText(/Describe one/));
    expect(useSessionStore.getState().pageTab).toBe("form");
  });

  it("surfaces a create failure", async () => {
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) =>
      init?.method === "POST"
        ? new Response(JSON.stringify({ detail: "An application named 'x' already exists" }), {
            status: 409,
            headers: { "Content-Type": "application/json" },
          })
        : new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );
    mount();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "x" } });
    fireEvent.change(screen.getByLabelText(/What problem/), { target: { value: "d" } });
    fireEvent.click(screen.getByRole("button", { name: /Start Build/ }));
    await waitFor(() => expect(screen.getByText(/already exists/)).toBeTruthy());
  });

  it("keeps a successful but conflicted creation pending through a failed confirmation, then opens on refresh", async () => {
    let finishWrite!: (response: Response) => void;
    let synchronized = false;
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return new Promise<Response>((resolve) => { finishWrite = resolve; });
      return synchronized ? json([application({ archived: false })]) : json({ detail: "offline" }, 503);
    });
    mount();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "x" } });
    fireEvent.change(screen.getByLabelText(/What problem/), { target: { value: "d" } });
    fireEvent.click(screen.getByRole("button", { name: /Start Build/ }));
    await act(async () => {
      applicationList.receive({ type: "application_updated", application: application({ id: "other", archived: false, session_id: null }) });
      finishWrite(json(application({ archived: false }), 201));
    });
    expect(await screen.findByText(/Created. Synchronizing/)).toBeTruthy();
    expect(await screen.findByText(/List not synchronized/)).toBeTruthy();
    expect(useSessionStore.getState().activeApplicationId).not.toBe("a1");
    expect((screen.getByRole("button", { name: /Starting/ }) as HTMLButtonElement).disabled).toBe(true);
    synchronized = true;
    fireEvent.click(screen.getByRole("button", { name: /Refresh applications/ }));
    await waitFor(() => expect(useSessionStore.getState().activeApplicationId).toBe("a1"));
    expect(fetchMock.mock.calls.filter(([, init]) => (init as RequestInit)?.method === "POST")).toHaveLength(1);
  });

  it("retains a confirmed creation across disconnect and opens it after reconnect synchronization", async () => {
    let finishWrite!: (response: Response) => void;
    let synchronized = false;
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return new Promise<Response>((resolve) => { finishWrite = resolve; });
      return synchronized ? json([application({ archived: false })]) : json({ detail: "offline" }, 503);
    });
    mount();
    const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
    await act(async () => { applicationList.connect(source); });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "x" } });
    fireEvent.change(screen.getByLabelText(/What problem/), { target: { value: "d" } });
    fireEvent.click(screen.getByRole("button", { name: /Start Build/ }));
    await act(async () => {
      applicationList.receive({ type: "application_updated", application: application({ id: "other", archived: false, session_id: null }) }, source);
      finishWrite(json(application({ archived: false }), 201));
    });
    expect(await screen.findByText(/Created. Synchronizing/)).toBeTruthy();
    act(() => { applicationList.disconnect(source); });
    expect(screen.getByText(/Created. Synchronizing/)).toBeTruthy();
    expect((screen.getByRole("button", { name: /Starting/ }) as HTMLButtonElement).disabled).toBe(true);
    synchronized = true;
    await act(async () => { applicationList.connect({ ...source, socket: {} }); });
    await waitFor(() => expect(useSessionStore.getState().activeApplicationId).toBe("a1"));
    expect(fetchMock.mock.calls.filter(([, init]) => (init as RequestInit)?.method === "POST")).toHaveLength(1);
  });

  it("confirms creation in the current connection when its success arrives after reconnect", async () => {
    let finishWrite!: (response: Response) => void;
    let created = false;
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return new Promise<Response>((resolve) => { finishWrite = resolve; });
      return json(created ? [application({ archived: false })] : []);
    });
    mount();
    const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
    await act(async () => { applicationList.connect(source); });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "x" } });
    fireEvent.change(screen.getByLabelText(/What problem/), { target: { value: "d" } });
    fireEvent.click(screen.getByRole("button", { name: /Start Build/ }));
    await act(async () => {
      applicationList.disconnect(source);
      applicationList.connect({ ...source, socket: {} });
    });
    await act(async () => {
      created = true;
      finishWrite(json(application({ archived: false }), 201));
    });
    await waitFor(() => expect(useSessionStore.getState().activeApplicationId).toBe("a1"));
    expect(fetchMock.mock.calls.filter(([, init]) => (init as RequestInit)?.method === "POST")).toHaveLength(1);
  });

  it.each(["account", "navigation"])("does not open a pending creation after %s changes", async (change) => {
    let finishWrite!: (response: Response) => void;
    let synchronized = false;
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return new Promise<Response>((resolve) => { finishWrite = resolve; });
      return synchronized ? json([application({ archived: false })]) : json({ detail: "offline" }, 503);
    });
    mount();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "x" } });
    fireEvent.change(screen.getByLabelText(/What problem/), { target: { value: "d" } });
    fireEvent.click(screen.getByRole("button", { name: /Start Build/ }));
    await act(async () => {
      applicationList.receive({ type: "application_updated", application: application({ id: "other", archived: false, session_id: null }) });
      finishWrite(json(application({ archived: false }), 201));
    });
    expect(await screen.findByText(/Created. Synchronizing/)).toBeTruthy();
    await act(async () => {
      if (change === "account") useSessionStore.setState({ accountGeneration: useSessionStore.getState().accountGeneration + 1 });
      else {
        useSessionStore.getState().showChat();
        useSessionStore.setState({ mainView: "application-create" });
      }
      synchronized = true;
      await applicationList.refresh();
    });
    await waitFor(() => expect(useSessionStore.getState().applications[0]?.id).toBe("a1"));
    expect(useSessionStore.getState().activeApplicationId).not.toBe("a1");
    expect(useSessionStore.getState().mainView).toBe("application-create");
    if (change === "account") expect(screen.queryByText(/Created. Synchronizing/)).toBeNull();
  });

  it("ends creation busy state after synchronization without taking back tab navigation", async () => {
    let finishWrite!: (response: Response) => void;
    let created = false;
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return new Promise<Response>((resolve) => { finishWrite = resolve; });
      return json(created && !String(url).includes("archived=true") ? [application({ archived: false })] : []);
    });
    mount();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "x" } });
    fireEvent.change(screen.getByLabelText(/What problem/), { target: { value: "d" } });
    fireEvent.click(screen.getByRole("button", { name: /Start Build/ }));
    fireEvent.click(screen.getByRole("button", { name: /^All/ }));
    await act(async () => {
      created = true;
      finishWrite(json(application({ archived: false }), 201));
    });
    await waitFor(() => expect(screen.getByText("Habit Tracker")).toBeTruthy());
    expect(useSessionStore.getState().mainView).toBe("application-create");
    expect(useSessionStore.getState().pageTab).toBe("all");
    fireEvent.click(screen.getByRole("button", { name: /^Create$/ }));
    expect(screen.queryByText(/Created. Synchronizing/)).toBeNull();
    expect((screen.getByRole("button", { name: /Start Build/ }) as HTMLButtonElement).disabled).toBe(false);
    expect(useSessionStore.getState().activeApplicationId).not.toBe("a1");
    expect(fetchMock.mock.calls.filter(([, init]) => (init as RequestInit)?.method === "POST")).toHaveLength(1);
  });

  it("disables All pin controls while a sidebar ordering operation is pending", async () => {
    const live = application({ archived: false });
    let finish!: (response: Response) => void;
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "PUT") return new Promise<Response>((resolve) => { finish = resolve; });
      return json(String(url).includes("archived=true") ? [] : [live]);
    });
    mount("all", [live]);
    let saving!: Promise<Application[]>;
    act(() => { saving = applicationList.reorder([live.id]); });
    expect((screen.getByLabelText("Unpin Habit Tracker from the sidebar") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByLabelText("Unpin Habit Tracker from the sidebar"));
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/unpin"))).toBe(false);
    await act(async () => { finish(json([live])); await saving; });
    expect((screen.getByLabelText("Unpin Habit Tracker from the sidebar") as HTMLButtonElement).disabled).toBe(false);
  });
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
