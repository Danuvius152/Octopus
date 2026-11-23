/**
 * The Schedules page's run history.
 *
 * "It ran at 07:18" is half an answer — what it *did* is in the session that
 * fire ran in, so the row opens it (native scheduling parity, VM0 links its
 * schedules to `lastRunId` for the same reason).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { SchedulesPage } from "./SchedulesPage";
import { SidebarManage } from "./SidebarManage";
import { useSessionStore, type Schedule } from "../stores/sessionStore";

const schedule = (o: Partial<Schedule> = {}): Schedule =>
  ({
    id: "sch1",
    agent_id: "ag1",
    name: "Daily digest",
    prompt: "summarize the inbox",
    interval_seconds: null,
    cron: "18 7 * * *",
    timezone: "America/Los_Angeles",
    recurrence_label: "Every day at 7:18 AM",
    enabled: true,
    created_at: "2026-01-01T00:00:00Z",
    last_run_at: new Date().toISOString(),
    origin_session_id: null,
    run_at: null,
    next_run_at: null,
    last_run_session_id: null,
    ...o,
  }) as Schedule;

function mount(schedules: Schedule[]) {
  useSessionStore.setState({
    token: "tok",
    schedules,
    agents: [
      { id: "ag1", name: "Octo", avatar: "🐙" } as unknown as ReturnType<
        typeof useSessionStore.getState
      >["agents"][number],
    ],
    sessions: [],
    activeSessionId: null,
  });
  return render(<SchedulesPage onToggleSidebar={() => {}} />);
}

beforeEach(() => {
  useSessionStore.setState({ token: "tok", accountGeneration: useSessionStore.getState().accountGeneration + 1 });
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response("[]", {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
    )
  );
});

afterEach(() => {
  cleanup();
  useSessionStore.getState().setToken("");
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("Schedule summaries", () => {
  const ineligible = [
    schedule({ id: "disabled", enabled: false, next_run_at: "2026-03-10T04:00:00Z" }),
    schedule({ id: "missing" }),
    schedule({ id: "invalid", next_run_at: "invalid time" }),
  ];

  it.each([
    {
      name: "choose the earliest instant across offsets",
      rows: [
        ...ineligible,
        schedule({ id: "later", next_run_at: "2026-03-10T05:00:00-02:00" }),
        schedule({ id: "earlier", next_run_at: "2026-03-10T14:00:00+08:00" }),
      ],
      next: new Date("2026-03-10T06:00:00Z"),
    },
    { name: "omit the next time when no schedule qualifies", rows: ineligible, next: null },
  ])("the page and sidebar $name", async ({ rows, next }) => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-03-08T00:00:00Z"));
    vi.mocked(fetch).mockImplementation(async (url) =>
      Response.json(String(url).endsWith("/api/schedules") ? rows : [])
    );
    render(<><SidebarManage /><SchedulesPage onToggleSidebar={() => {}} /></>);

    const count = rows.length;
    const heading = await screen.findByText(new RegExp(`^${count} schedules`));
    const sidebar = screen.getByRole("button", { name: /^Schedules/ });
    const label = next
      ? ` · next ${next.toLocaleDateString([], { weekday: "short" })} ${next.toLocaleTimeString([], {
        hour: "2-digit", minute: "2-digit", hour12: false,
      })}`
      : "";
    expect(heading.textContent).toBe(`${count} schedules${label}`);
    expect(sidebar.textContent).toBe(`Schedules${count}${label}`);
  });
});

describe("SchedulesPage run history", () => {
  it("opens the session a run happened in", async () => {
    const { container } = mount([
      schedule({ last_run_session_id: "sess-42" }),
    ]);
    // The card starts collapsed; open it to reach the run history.
    fireEvent.click(screen.getByText("Daily digest"));

    const row = container.querySelector(".schedule-last-run");
    expect(row).toBeTruthy();
    fireEvent.click(row!);

    await waitFor(() =>
      expect(useSessionStore.getState().activeSessionId).toBe("sess-42")
    );
  });

  it("leaves the row inert when the fire predates the link", () => {
    // Rows that ran before the column existed still say when they ran; they
    // just have nothing to open.
    const { container } = mount([schedule({ last_run_session_id: null })]);
    fireEvent.click(screen.getByText("Daily digest"));
    expect(container.querySelector(".schedule-last-run")).toBeNull();
    expect(screen.getByText(/last run/)).toBeTruthy();
  });
});

describe("Schedule knowledge controls", () => {
  it("lets a human authorize a paused legacy task without enabling it", async () => {
    const old = schedule({ enabled: false, invalid_reason: "knowledge_contract_missing" });
    let stored = old;
    vi.mocked(fetch).mockImplementation(async (_url, init) => {
      if (init?.method === "PATCH") {
        expect(JSON.parse(String(init.body))).toEqual({ wiki_mode: "read" });
        stored = { ...old, wiki_mode: "read", invalid_reason: null };
        return Response.json(stored);
      }
      return Response.json([stored]);
    });
    mount([old]);
    await waitFor(() => expect(screen.getByRole("switch").hasAttribute("disabled")).toBe(true));
    fireEvent.change(screen.getByLabelText("Wiki maintenance for Daily digest"), { target: { value: "read" } });
    await waitFor(() => expect(screen.getByRole("switch").hasAttribute("disabled")).toBe(false));
    expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("false");
  });

  it("shows a rejected control change without applying it locally", async () => {
    const old = schedule({ wiki_mode: "read" });
    vi.mocked(fetch).mockImplementation(async (_url, init) =>
      init?.method === "PATCH"
        ? Response.json({ detail: "The original scope is unavailable" }, { status: 422 })
        : Response.json([old])
    );
    mount([old]);
    fireEvent.change(screen.getByLabelText("Wiki maintenance for Daily digest"), { target: { value: "auto" } });
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("The original scope is unavailable"));
    expect((screen.getByLabelText("Wiki maintenance for Daily digest") as HTMLSelectElement).value).toBe("read");
  });
});
