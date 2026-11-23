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
import { deferred } from "../test-support/deferred";

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
    deletion: null,
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
  vi.useRealTimers();
});

describe("ApplicationFormPage", () => {
  it("freezes every creation field through POST and target confirmation without creating twice", async () => {
    let finishWrite!: (response: Response) => void;
    let finishRead!: (response: Response) => void;
    const live = application({ archived: false });
    let listed: Application[] = [];
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return new Promise<Response>(resolve => { finishWrite = resolve; });
      if (String(url).endsWith("/applications/a1")) return new Promise<Response>(resolve => { finishRead = resolve; });
      return json(listed);
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
      applicationList.receive({ type: "application_archived", application_id: "a1" });
      finishWrite(json(application({ archived: false }), 201));
    });
    expect(finishRead).toBeDefined();
    expectFrozen();
    await act(async () => { listed = [live]; finishRead(json(live)); });
    await waitFor(() => expect(useSessionStore.getState().activeApplicationId).toBe("a1"));
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });

  it("retires the Create tab operation on leaving for All", async () => {
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
    expect(screen.getByLabelText("Archive Habit Tracker")).toBeEnabled();
    expect(screen.getByLabelText("Unpin Habit Tracker from the sidebar")).toBeEnabled();
    expect(screen.getByRole("button", { name: "Open" })).toBeEnabled();
    await act(async () => finish(json({ detail: "Creation refused" }, 409)));
    expect(screen.queryByText("Creation refused")).toBeNull();
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
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/applications/a1"))).toHaveLength(0);
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

  it("settles a live 201 captured before an observed deletion", async () => {
    let finish!: (response: Response) => void;
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) =>
      init?.method === "POST" ? new Promise<Response>(resolve => { finish = resolve; }) : json([]));
    mount();
    startBuild();
    await act(async () => {
      applicationList.receive({ type: "application_deleted", application_id: "a1" });
      finish(json(application({ archived: false, deletion: null }), 201));
    });

    expect(screen.getByText(/application was deleted/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Starting/ })).toBeNull();
    expect(useSessionStore.getState().applications).toEqual([]);
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/applications/a1"))).toHaveLength(0);
  });

  it("reports a marked creation receipt without opening or reading it again", async () => {
    const deleting = application({ archived: false, deletion: { phase: "retiring", keep_files: true, error: null } });
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) =>
      init?.method === "POST" ? json(deleting, 201) : json([deleting]));
    mount();
    await act(async () => startBuild());
    expect(screen.getByText("The application is being deleted.")).toBeTruthy();
    expect(useSessionStore.getState().applications).toEqual([deleting]);
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/applications/a1"))).toHaveLength(0);
  });

  it("prefers a marked creation receipt to an earlier shared live snapshot", async () => {
    const live = application({ archived: false });
    const deleting = { ...live, deletion: { phase: "retiring", keep_files: true, error: null } } as const;
    let finishWrite!: (response: Response) => void;
    const listed = { ...live, name: "Updated name" };
    let listFailed = false;
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) =>
      init?.method === "POST" ? new Promise<Response>(resolve => { finishWrite = resolve; })
        : listFailed ? json({}, 503) : json([listed]));
    mount();
    startBuild();
    await act(async () => { await applicationList.refresh(); });
    expect(useSessionStore.getState().applications).toEqual([listed]);
    await act(async () => { listFailed = true; finishWrite(json(deleting, 201)); });
    expect(screen.getByText("The application is being deleted.")).toBeTruthy();
    expect(useSessionStore.getState().applications).toEqual([{ ...listed, deletion: deleting.deletion }]);
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
  });

  it.each(["POST", "target GET"])("keeps a marked %s from restoring a row after the live list confirms absence", async (source) => {
    const creation = deferred<Response>();
    const target = deferred<Response>();
    const live = application({ archived: false, session_id: null });
    const deleting = { ...live, deletion: { phase: "retiring", keep_files: true, error: null } } as const;
    let listFailed = true;
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return creation.promise;
      if (String(url).endsWith("/applications/a1")) return target.promise;
      return listFailed ? json({}, 503) : json([]);
    });
    mount();
    startBuild();
    await act(async () => {
      if (source === "POST") applicationList.receive({ type: "application_created", application: live });
      else {
        applicationList.receive({ type: "application_archived", application_id: "a1" });
        creation.resolve(json(live, 201));
      }
    });
    const targetReads = () => fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/applications/a1"));
    expect(targetReads()).toHaveLength(source === "POST" ? 0 : 1);
    listFailed = false;
    await act(async () => { expect(await applicationList.refresh()).toEqual({ state: "applied" }); });
    listFailed = true;
    await act(async () => {
      if (source === "POST") creation.resolve(json(deleting, 201));
      else target.resolve(json(deleting));
    });
    expect(screen.getByText("The application is being deleted.")).toBeTruthy();
    expect(useSessionStore.getState()).toMatchObject({ applications: [], archivedApplications: [], activeApplicationId: null });
    expect(targetReads()).toHaveLength(source === "POST" ? 0 : 1);
  });

  it("confirms membership when an archived creation receipt and a shared live snapshot disagree", async () => {
    const live = application({ archived: false });
    const archived = application();
    let finishWrite!: (response: Response) => void;
    let listed: Application[] = [live];
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return new Promise<Response>(resolve => { finishWrite = resolve; });
      return json(String(url).endsWith("/applications/a1") ? archived : listed);
    });
    mount();
    startBuild();
    await act(async () => { await applicationList.refresh(); });
    await act(async () => { listed = []; finishWrite(json(archived, 201)); });
    expect(screen.getByText("The application was archived.")).toBeTruthy();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/applications/a1"))).toHaveLength(1);
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
  });

  it("keeps a confirmed deletion marker through an older update and live 201", async () => {
    const live = application({ archived: false });
    const deleting = { ...live, deletion: { phase: "retiring", keep_files: true, error: null } } as const;
    let finishWrite!: (response: Response) => void;
    let listed: Application[] = [deleting];
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) =>
      init?.method === "POST" ? new Promise<Response>(resolve => { finishWrite = resolve; }) : json(listed));
    mount();
    startBuild();
    await act(async () => { await applicationList.refresh(); });
    expect(useSessionStore.getState().applications).toEqual([deleting]);
    await act(async () => {
      applicationList.receive({ type: "application_updated", application: live });
      expect(useSessionStore.getState().applications).toEqual([deleting]);
      listed = [live];
      finishWrite(json(live, 201));
    });
    expect(screen.getByText("The application is being deleted.")).toBeTruthy();
    expect(useSessionStore.getState().applications).toEqual([deleting]);
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
  });

  it.each(["shared live snapshot", "archive hint"])("keeps a target 404 across a competing %s", async (conflict) => {
    const live = application({ archived: false });
    let finishWrite!: (response: Response) => void;
    let finishList!: (response: Response) => void;
    let finishTarget!: (response: Response) => void;
    let targetSignal: AbortSignal | null | undefined;
    let listReads = 0;
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return new Promise<Response>(resolve => { finishWrite = resolve; });
      if (String(url).endsWith("/applications/a1")) {
        targetSignal = init?.signal;
        return new Promise<Response>(resolve => { finishTarget = resolve; });
      }
      if (++listReads === 1 && conflict === "shared live snapshot") {
        return new Promise<Response>(resolve => { finishList = resolve; });
      }
      return json([]);
    });
    mount();
    startBuild();
    await act(async () => {
      applicationList.receive({ type: "application_archived", application_id: "a1" });
      finishWrite(json(live, 201));
    });
    expect(finishTarget).toBeDefined();
    if (conflict === "shared live snapshot") {
      await act(async () => finishList(json([live])));
      expect(targetSignal?.aborted).toBe(false);
    } else {
      await act(async () => applicationList.receive({ type: "application_archived", application_id: "a1" }));
    }
    await act(async () => finishTarget(json({ detail: "Application not found" }, 404)));
    expect(screen.getByText("The application was deleted.")).toBeTruthy();
    expect(useSessionStore.getState().applications).toEqual([]);
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
  });

  it.each(["archived", "deleted"])("uses a later-started target GET to settle %s", async (outcome) => {
    const live = application({ archived: false });
    const earlier = outcome === "archived" ? live : application({
      archived: false, deletion: { phase: "retiring", keep_files: true, error: null },
    });
    let finishWrite!: (response: Response) => void;
    let finishList!: (response: Response) => void;
    let finishTarget!: (response: Response) => void;
    let targetSignal: AbortSignal | null | undefined;
    let listReads = 0;
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return new Promise<Response>(resolve => { finishWrite = resolve; });
      if (String(url).endsWith("/applications/a1")) {
        targetSignal = init?.signal;
        return new Promise<Response>(resolve => { finishTarget = resolve; });
      }
      if (++listReads === 1) return new Promise<Response>(resolve => { finishList = resolve; });
      return json([]);
    });
    mount();
    startBuild();
    await act(async () => applicationList.receive({ type: "application_archived", application_id: "a1" }));
    expect(finishList).toBeDefined();
    await act(async () => finishWrite(json(live, 201)));
    expect(finishTarget).toBeDefined();
    await act(async () => finishList(json([earlier])));
    expect(targetSignal?.aborted).toBe(false);
    await act(async () => finishTarget(outcome === "archived" ? json(application()) : json({}, 404)));
    expect(screen.getByText(`The application was ${outcome}.`)).toBeTruthy();
    expect(useSessionStore.getState().applications).toEqual([]);
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
  });

  it("uses the target archived row after a late live metadata update", async () => {
    const live = application({ archived: false });
    let finishWrite!: (response: Response) => void;
    let finishTarget!: (response: Response) => void;
    let listReads = 0;
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return new Promise<Response>(resolve => { finishWrite = resolve; });
      if (String(url).endsWith("/applications/a1")) return new Promise<Response>(resolve => { finishTarget = resolve; });
      if (++listReads === 1) return json([live]);
      return new Promise<Response>(() => {});
    });
    mount();
    startBuild();
    await act(async () => { await applicationList.refresh(); });
    await act(async () => finishWrite(json(application(), 201)));
    expect(finishTarget).toBeDefined();
    await act(async () => applicationList.receive({
      type: "application_updated", application: { ...live, status: "building", session_id: null },
    }));
    expect(useSessionStore.getState().applications[0]?.status).toBe("building");
    await act(async () => finishTarget(json(application())));
    expect(screen.getByText("The application was archived.")).toBeTruthy();
    expect(useSessionStore.getState().applications).toEqual([]);
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
  });

  it.each(["live", "archived", "deleted"])("reconciles a target %s with a newer live list missing the known ID", async (outcome) => {
    const creation = deferred<Response>();
    const retry = deferred<Response>();
    const live = application({ archived: false });
    let targetReads = 0;
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return creation.promise;
      if (!String(url).endsWith("/applications/a1")) return json([]);
      return ++targetReads === 1 ? json({}, 503) : retry.promise;
    });
    mount();
    startBuild();
    await act(async () => {
      applicationList.receive({ type: "application_archived", application_id: "a1" });
      creation.resolve(json(live, 201));
    });
    expect(screen.getByText(/Application a1 was created/)).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Check application again" })));
    expect(targetReads).toBe(2);
    await act(async () => { expect(await applicationList.refresh()).toEqual({ state: "applied" }); });
    await act(async () => retry.resolve(outcome === "deleted" ? json({}, 404)
      : json(outcome === "archived" ? application() : live)));
    const message = outcome === "live" ? /Application a1 was created/ : `The application was ${outcome}.`;
    expect(screen.getByText(message)).toBeTruthy();
    expect(useSessionStore.getState().applications).toEqual([]);
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });

  it.each([503, 404])("settles a target %i after accepting an earlier archived snapshot", async (status) => {
    const creation = deferred<Response>();
    const archivedList = deferred<Response>();
    const target = deferred<Response>();
    const live = application({ archived: false });
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return creation.promise;
      if (String(url).endsWith("/applications/a1")) return target.promise;
      return String(url).includes("archived=true") ? archivedList.promise : json([]);
    });
    mount();
    startBuild();
    await act(async () => applicationList.receive({ type: "application_archived", application_id: "a1" }));
    await act(async () => { void applicationList.refresh("archived"); });
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("archived=true"))).toBe(true);
    await act(async () => creation.resolve(json(live, 201)));
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/applications/a1"))).toBe(true);
    await act(async () => archivedList.resolve(json([application()])));
    expect(useSessionStore.getState().archivedApplications.map(row => row.id)).toEqual(["a1"]);
    await act(async () => target.resolve(json({}, status)));
    expect(screen.getByText(status === 404 ? "The application was deleted." : "The application was archived.")).toBeTruthy();
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
  });

  it.each(["live", "archived"])("preserves WS fields when the target GET confirms %s membership", async (membership) => {
    const creation = deferred<Response>();
    const target = deferred<Response>();
    const live = application({ archived: false, status: "building", session_id: null });
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return creation.promise;
      return String(url).endsWith("/applications/a1") ? target.promise : json({}, 503);
    });
    mount("form", [live]);
    startBuild();
    await act(async () => {
      applicationList.receive({ type: "application_archived", application_id: "a1" });
      creation.resolve(json(live, 201));
    });
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/applications/a1"))).toBe(true);
    await act(async () => applicationList.receive({
      type: "application_updated", application: { ...live, status: "ready", name: "Updated by WS" },
    }));
    const archived = membership === "archived";
    await act(async () => target.resolve(json({ ...live, archived, pinned: !archived })));
    const state = useSessionStore.getState();
    const result = (archived ? state.archivedApplications : state.applications)[0];
    expect(result).toMatchObject({ id: "a1", status: "ready", name: "Updated by WS", archived, pinned: !archived });
    expect(state.activeApplicationId).toBe(archived ? null : "a1");
    if (archived) expect(screen.getByText("The application was archived.")).toBeTruthy();
  });

  it("confirms the target after a live POST conflicts with an earlier archived snapshot", async () => {
    const creation = deferred<Response>();
    const live = application({ archived: false });
    let listed = false;
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return creation.promise;
      if (String(url).endsWith("/applications/a1")) return json(live);
      if (String(url).includes("archived=true")) return json(listed ? [] : [application()]);
      return json(listed ? [live] : []);
    });
    mount();
    startBuild();
    await act(async () => { await applicationList.refresh("archived"); });
    await act(async () => { listed = true; creation.resolve(json(live, 201)); });
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/applications/a1"))).toHaveLength(1);
    expect(useSessionStore.getState().activeApplicationId).toBe("a1");
  });

  it("checks a late live 201 after the app disappeared from the archived list", async () => {
    const creation = deferred<Response>();
    const live = application({ archived: false });
    let archived: Application[] = [application()];
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return creation.promise;
      if (String(url).endsWith("/applications/a1")) return json({}, 404);
      return json(String(url).includes("archived=true") ? archived : []);
    });
    mount();
    startBuild();
    await act(async () => { await applicationList.refresh("archived"); });
    expect(useSessionStore.getState().archivedApplications.map(row => row.id)).toEqual(["a1"]);
    archived = [];
    await act(async () => { await applicationList.refresh("archived"); });
    await act(async () => creation.resolve(json(live, 201)));
    expect(screen.getByText("The application was deleted.")).toBeTruthy();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/applications/a1"))).toHaveLength(1);
    expect(useSessionStore.getState().applications).toEqual([]);
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
  });

  it("does not apply a target live GET started before an archived appearance and removal", async () => {
    const creation = deferred<Response>();
    const target = deferred<Response>();
    const live = application({ archived: false });
    let archived: Application[] = [application()];
    let targetReads = 0;
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return creation.promise;
      if (String(url).endsWith("/applications/a1")) return ++targetReads === 1 ? json({}, 503) : target.promise;
      return json(String(url).includes("archived=true") ? archived : []);
    });
    mount();
    startBuild();
    await act(async () => {
      applicationList.receive({ type: "application_archived", application_id: "a1" });
      creation.resolve(json(live, 201));
    });
    expect(screen.getByText(/Application a1 was created/)).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Check application again" })));
    expect(targetReads).toBe(2);
    await act(async () => { await applicationList.refresh("archived"); });
    expect(useSessionStore.getState().archivedApplications.map(row => row.id)).toEqual(["a1"]);
    archived = [];
    await act(async () => { await applicationList.refresh("archived"); });
    await act(async () => target.resolve(json(live)));
    expect(screen.getByText(/Application a1 was created/)).toBeTruthy();
    expect(useSessionStore.getState().applications).toEqual([]);
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
  });

  it.each([true, false])("checks the 201 against list absence after a creation event when absent=%s", async (absent) => {
    const creation = deferred<Response>();
    const list = deferred<Response>();
    const live = application({ archived: false, session_id: null });
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return creation.promise;
      if (String(url).endsWith("/applications/a1")) return json({}, 404);
      return list.promise;
    });
    mount();
    startBuild();
    await act(async () => applicationList.receive({ type: "application_created", application: live }));
    expect(fetchMock.mock.calls.some(([url, init]) => String(url).endsWith("/api/applications") && !init?.method)).toBe(true);
    if (absent) await act(async () => list.resolve(json([])));
    await act(async () => creation.resolve(json(live, 201)));
    if (absent) expect(screen.getByText("The application was deleted.")).toBeTruthy();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/applications/a1"))).toHaveLength(absent ? 1 : 0);
    expect(useSessionStore.getState().activeApplicationId).toBe(absent ? null : "a1");
    if (!absent) await act(async () => list.resolve(json([live])));
  });

  it.each([true, false])("preserves confirmed archive feedback when an explicit check fails with archive hint=%s", async (hint) => {
    const target = deferred<Response>();
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return json(application(), 201);
      return String(url).endsWith("/applications/a1") ? target.promise : json([]);
    });
    mount();
    await act(async () => startBuild());
    expect(screen.getByText("The application was archived.")).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Check application again" })));
    if (hint) await act(async () => applicationList.receive({ type: "application_archived", application_id: "a1" }));
    await act(async () => target.resolve(json({}, 503)));
    expect(screen.getByText("The application was archived.")).toBeTruthy();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/applications/a1"))).toHaveLength(1);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });

  it("accepts a deletion marker on an archive frame while an older archived GET is pending", async () => {
    const creation = deferred<Response>();
    const target = deferred<Response>();
    const archived = application({ session_id: null });
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return creation.promise;
      return String(url).endsWith("/applications/a1") ? target.promise : json({}, 503);
    });
    mount();
    startBuild();
    await act(async () => {
      applicationList.receive({ type: "application_archived", application_id: "a1" });
      creation.resolve(json({ ...archived, archived: false }, 201));
    });
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/applications/a1"))).toHaveLength(1);
    await act(async () => applicationList.receive({
      type: "application_archived", application: { ...archived, deletion: { phase: "retiring", keep_files: true, error: null } },
    }));
    await act(async () => target.resolve(json(archived)));
    expect(screen.getByText("The application is being deleted.")).toBeTruthy();
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
  });

  it("preserves a confirmed deletion marker that preceded any list row when rechecking fails", async () => {
    const creation = deferred<Response>();
    const live = application({ archived: false, session_id: null });
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) =>
      init?.method === "POST" ? creation.promise : json({}, 503));
    mount();
    startBuild();
    await act(async () => applicationList.receive({
      type: "application_updated", application: { ...live, deletion: { phase: "retiring", keep_files: true, error: null } },
    }));
    await act(async () => creation.resolve(json(live, 201)));
    expect(screen.getByText("The application is being deleted.")).toBeTruthy();
    expect(useSessionStore.getState().applications).toEqual([]);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Check application again" })));
    expect(screen.getByText("The application is being deleted.")).toBeTruthy();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/applications/a1"))).toHaveLength(1);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });

  it.each(["restore", "archive"])("settles a failed recheck after a %s hint between requests", async (hint) => {
    const archived = application({ session_id: null });
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) =>
      init?.method === "POST" ? json(archived, 201) : json({}, 503));
    mount();
    await act(async () => startBuild());
    expect(screen.getByText("The application was archived.")).toBeTruthy();
    await act(async () => {
      if (hint === "restore") applicationList.receive({ type: "application_updated", application: { ...archived, archived: false } });
      else applicationList.receive({ type: "application_archived", application_id: "a1" });
    });
    expect(useSessionStore.getState().archivedApplications.map(row => row.id)).toEqual(["a1"]);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Check application again" })));
    expect(screen.getByText(hint === "restore" ? /Application a1 was created/ : "The application was archived.")).toBeTruthy();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/applications/a1"))).toHaveLength(1);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });

  it("retires an older archive observation when a restored target row is blocked by a later live absence", async () => {
    const target = deferred<Response>();
    const archived = application();
    let targetReads = 0;
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return json(archived, 201);
      if (String(url).endsWith("/applications/a1")) return ++targetReads === 1 ? target.promise : json({}, 503);
      return json([]);
    });
    mount();
    await act(async () => startBuild());
    expect(screen.getByText("The application was archived.")).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Check application again" })));
    expect(targetReads).toBe(1);
    await act(async () => { expect(await applicationList.refresh()).toEqual({ state: "applied" }); });
    await act(async () => target.resolve(json({ ...archived, archived: false })));
    expect(screen.getByText(/Application a1 was created/)).toBeTruthy();
    expect(useSessionStore.getState().applications).toEqual([]);
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Check application again" })));
    expect(screen.getByText(/Application a1 was created/)).toBeTruthy();
    expect(targetReads).toBe(2);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });

  it.each(["WS", "DELETE"])("settles a captured live target GET after a newer %s removal", async (source) => {
    const live = application({ archived: false });
    let finishWrite!: (response: Response) => void;
    let finishBody!: (row: Application) => void;
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return new Promise<Response>(resolve => { finishWrite = resolve; });
      if (init?.method === "DELETE") return new Response(null, { status: 204 });
      if (String(url).endsWith("/applications/a1")) {
        return { ok: true, json: () => new Promise(resolve => { finishBody = resolve; }) } as Response;
      }
      return json([]);
    });
    mount();
    startBuild();
    await act(async () => {
      applicationList.receive({ type: "application_archived", application_id: "a1" });
      finishWrite(json(live, 201));
    });
    expect(finishBody).toBeDefined();
    await act(async () => {
      if (source === "WS") applicationList.receive({ type: "application_deleted", application_id: "a1" });
      else await applicationList.remove("a1", false);
    });
    expect(screen.getByText("The application was deleted.")).toBeTruthy();
    await act(async () => finishBody(live));
    expect(useSessionStore.getState().applications).toEqual([]);
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });

  it("gives a known ID its own read budget and requires an explicit read retry", async () => {
    vi.useFakeTimers();
    const live = application({ archived: false });
    let finishWrite!: (response: Response) => void;
    let finishBody!: (row: Application) => void;
    let reads = 0;
    let listed: Application[] = [];
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return new Promise<Response>(resolve => { finishWrite = resolve; });
      if (!String(url).endsWith("/applications/a1")) return json(listed);
      if (++reads === 1) return { ok: true, json: () => new Promise(resolve => { finishBody = resolve; }) } as Response;
      return json(live);
    });
    mount();
    startBuild();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(29_000);
      applicationList.receive({ type: "application_archived", application_id: "a1" });
      finishWrite(json(live, 201));
    });
    expect(finishBody).toBeDefined();
    await act(async () => vi.advanceTimersByTimeAsync(29_000));
    expect(screen.getByRole("button", { name: /Starting/ })).toBeDisabled();
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(screen.getByText(/Application a1 was created/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /Start Build/ })).toBeDisabled();
    await act(async () => {
      finishBody(live);
      listed = [live];
      await applicationList.refresh();
    });
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Check application again" })));
    expect(useSessionStore.getState().activeApplicationId).toBe("a1");
    expect(reads).toBe(2);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });

  it.each(["headers", "body"])("times out POST %s across a same-account reconnect", async (phase) => {
    vi.useFakeTimers();
    const live = application({ archived: false });
    let deliverLate!: () => void;
    let signal: AbortSignal | null | undefined;
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) => {
      if (init?.method !== "POST") return json([]);
      signal = init.signal;
      if (phase === "headers") return new Promise<Response>(resolve => { deliverLate = () => resolve(json(live, 201)); });
      return { ok: true, json: () => new Promise(resolve => { deliverLate = () => resolve(live); }) } as Response;
    });
    mount();
    const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
    await act(async () => applicationList.connect(source));
    startBuild();
    await act(async () => {
      applicationList.disconnect(source);
      applicationList.connect({ ...source, socket: {} });
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(screen.getByText(/outcome is unknown.*check the applications list/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /Start Build/ })).toBeEnabled();
    expect(signal?.aborted).toBe(true);
    await act(async () => deliverLate());
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
    expect(useSessionStore.getState().applications).toEqual([]);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });

  it.each(["row", "read"])("uses a current %s when its success arrives after reconnect", async (evidence) => {
    const live = application({ archived: false });
    let finishWrite!: (response: Response) => void;
    let listed: Application[] = [];
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return new Promise<Response>(resolve => { finishWrite = resolve; });
      return json(String(url).endsWith("/applications/a1") ? live : listed);
    });
    mount();
    const source = { socket: {}, accountGeneration: useSessionStore.getState().accountGeneration };
    await act(async () => applicationList.connect(source));
    startBuild();
    await act(async () => {
      applicationList.disconnect(source);
      if (evidence === "row") listed = [live];
      applicationList.connect({ ...source, socket: {} });
    });
    await act(async () => {
      finishWrite(json(live, 201));
      listed = [live];
    });
    expect(useSessionStore.getState().activeApplicationId).toBe("a1");
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/applications/a1"))).toHaveLength(evidence === "read" ? 1 : 0);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });

  it.each(["account", "navigation"])("retires a pending creation after %s changes", async (change) => {
    let finishWrite!: (response: Response) => void;
    const live = application({ archived: false });
    let listed: Application[] = [];
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) =>
      init?.method === "POST" ? new Promise<Response>(resolve => { finishWrite = resolve; }) : json(listed));
    mount();
    startBuild();
    await act(async () => {
      if (change === "account") useSessionStore.setState({ accountGeneration: useSessionStore.getState().accountGeneration + 1 });
      else {
        useSessionStore.getState().showChat();
        useSessionStore.setState({ mainView: "application-create" });
        listed = [live];
      }
      finishWrite(json(live, 201));
    });
    expect(useSessionStore.getState().applications.map(row => row.id)).toEqual(change === "account" ? [] : ["a1"]);
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
    expect(screen.getByRole("button", { name: /Start Build/ })).toBeEnabled();
  });

  it.each([201, 409])("leaves the replacement form's pending request alone after a late %s", async (status) => {
    const finishes: ((response: Response) => void)[] = [];
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) =>
      init?.method === "POST" ? new Promise<Response>(resolve => { finishes.push(resolve); }) : json([]));
    mount();
    startBuild();
    fireEvent.click(screen.getByRole("button", { name: /^All/ }));
    fireEvent.click(screen.getByRole("button", { name: /^Create$/ }));
    startBuild();
    expect(finishes).toHaveLength(2);
    await act(async () => finishes[0](status === 201
      ? json(application({ archived: false }), 201) : json({ detail: "Previous request refused" }, 409)));
    expect(screen.getByRole("button", { name: /Starting/ })).toBeDisabled();
    expect(screen.queryByText("Previous request refused")).toBeNull();
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
    await act(async () => finishes[1](json({ detail: "Current request refused" }, 409)));
    expect(screen.getByText("Current request refused")).toBeTruthy();
  });

  it.each(["archived", "deleting", "deleted"])("settles a target read as %s", async (state) => {
    let finishWrite!: (response: Response) => void;
    const live = application({ archived: false });
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "POST") return new Promise<Response>(resolve => { finishWrite = resolve; });
      if (!String(url).endsWith("/applications/a1")) return json([]);
      if (state === "deleted") return json({ detail: "Application not found" }, 404);
      if (state === "archived") return json({ ...live, archived: true });
      return json({ ...live, deletion: { phase: "retiring", error: null, keep_files: true } });
    });
    mount();
    startBuild();
    await act(async () => {
      applicationList.receive({ type: "application_archived", application: application() });
      finishWrite(json(live, 201));
    });
    const label = state === "deleting" ? /application is being deleted/i : new RegExp("application was " + state, "i");
    expect(screen.getByText(label)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Starting/ })).toBeNull();
    expect(useSessionStore.getState().activeApplicationId).toBeNull();
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

function startBuild() {
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Tracker" } });
  fireEvent.change(screen.getByLabelText(/What problem/), { target: { value: "Track habits" } });
  fireEvent.click(screen.getByRole("button", { name: /Start Build/ }));
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
