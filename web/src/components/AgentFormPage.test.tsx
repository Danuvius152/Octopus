/**
 * The Agents page — the Create/Edit form (create, edit, archive) and the All
 * tab (sidebar-pins.md §6): every agent by section, pin toggles, Chat, and
 * restore from the Archived section.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";

import { AgentFormPage } from "./AgentFormPage";
import { handleWsMessage, useWebSocket } from "../hooks/useWebSocket";
import { connectorInstallations } from "../lib/connectorInstallations";
import { backendCatalogFixture } from "../test-support/backendCatalog";
import { deferred } from "../test-support/deferred";
import { Socket } from "../test-support/socket";
import { refreshAgents } from "../lib/refreshAgents";
import { showSession } from "../lib/selectSession";
import { SidebarAgents } from "./SidebarAgents";
import {
  useSessionStore, type Agent, type BackendKind, type SessionInfo,
} from "../stores/sessionStore";

function agent(o: Partial<Agent> = {}): Agent {
  return {
    id: "ag1",
    name: "Octo",
    description: "",
    avatar: "🐙",
    system_prompt: "",
    model: null,
    credential_id: null,
    backend: "claude-code",
    mcp_servers: ["ask", "bg"],
    tool_allow: "",
    tool_deny: "",
    is_system: true,
    archived: false,
    pinned: true,
    pin_order: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    active_session_count: 0,
    subagents: [],
    ...o,
  };
}

let fetchMock: ReturnType<typeof vi.fn>;

function mount(
  editingAgentId: string | null = null,
  agents: Agent[] = [agent()],
  pageTab: "all" | "form" = "form",
  defaultBackend: BackendKind = "claude-code",
) {
  useSessionStore.setState({
    token: "tok",
    agents,
    sessions: [],
    editingAgentId,
    pageTab,
    credentials: [],
    connectorInstallations: [],
    backendCatalog: { status: "ready", catalog: backendCatalogFixture(undefined, defaultBackend) },
    mainView: "agent-form",
  });
  return render(<AgentFormPage onToggleSidebar={() => {}} />);
}

beforeEach(() => {
  useSessionStore.setState({ ...useSessionStore.getInitialState(), accountGeneration: useSessionStore.getState().accountGeneration + 1 });
  fetchMock = vi.fn(async (url: unknown) => emptyRead(url));
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("confirm", vi.fn(() => true));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AgentFormPage", () => {
  it("groups model defaults while keeping agent identity, sign-in and tools in the edit form", async () => {
    fetchMock.mockImplementation(async (url: unknown) => String(url).includes("/models")
      ? json({ models: [{ id: "sonnet", name: "Claude Sonnet", reasoning_efforts: ["low"], service_tiers: [] }] })
      : emptyRead(url));
    mount("ag1", [agent({ model: "saved-claude-model" })]);

    expect(screen.getByLabelText("Default sign-in")).toBeVisible();
    expect(screen.getByLabelText("System prompt")).toBeVisible();
    expect(screen.getByLabelText("Allowed tools")).toBeVisible();
    expect(screen.getByLabelText("Denied tools")).toBeVisible();
    expect(screen.getByText("Sub-agents", { exact: true })).toBeVisible();
    const disclosure = screen.getByText("Model defaults", { exact: true }).closest("details");
    expect(disclosure).not.toHaveAttribute("open");
    fireEvent.click(screen.getByText("Model defaults", { exact: true }));
    const model = await screen.findByRole("combobox", { name: "Model" });
    expect(model).toHaveValue("saved-claude-model");
    expect(await screen.findByRole("option", { name: "Claude Sonnet" })).toHaveValue("sonnet");
    expect(screen.getByRole("option", { name: "saved-claude-model · outside catalog" })).toBeVisible();
  });

  it("clears saved Agent model defaults when the catalog fails", async () => {
    const saved = agent({ backend: "codex", model: "old", reasoning_effort: "high", service_tier: "fast" });
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (String(url).includes("/models")) return json({ models: [], default_model: "cli-model", catalog_error: "Cannot read catalog" });
      if (init?.method === "PATCH") return json({ ...saved, ...JSON.parse(String(init.body)) });
      return emptyRead(url);
    });
    mount(saved.id, [saved]);
    expect(await screen.findByRole("button", { name: "Retry catalog" })).toBeEnabled();
    fireEvent.click(screen.getByText("Model defaults", { exact: true }));
    expect(screen.getByRole("option", { name: "Engine default (cli-model)" })).toBeEnabled();
    for (const label of ["Model", "Reasoning effort", "Speed"]) {
      const control = screen.getByLabelText(label);
      expect(control).toBeEnabled();
      fireEvent.change(control, { target: { value: "" } });
    }
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));
    await waitFor(() => expect(patchBody()).toEqual({ model: null, reasoning_effort: null, service_tier: null }));
  });

  it("opens Model defaults and focuses the control rejected on save", async () => {
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (String(url).endsWith("/models")) return json({ models: [], reasoning_efforts: ["low"] });
      if (init?.method === "PATCH") return json({ detail: { field: "reasoning_effort", message: "Choose an available effort." } }, 400);
      return emptyRead(url);
    });
    mount("ag1", [agent({ reasoning_effort: "high" })]);
    const disclosure = screen.getByText("Model defaults", { exact: true }).closest("details");
    expect(disclosure).not.toHaveAttribute("open");
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));
    expect(await screen.findByText("Choose an available effort.")).toBeVisible();
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Reasoning effort" })).toHaveFocus());
    expect(disclosure).toHaveAttribute("open");
  });

  it("freezes every editor and new configuration write during one synchronously admitted save", async () => {
    const existing = agent({ is_system: false, backend: "codex", subagents: [{ name: "reviewer", description: "Review", prompt: "Inspect", model: null, tools: [] }] });
    const reply = deferred<Response>();
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => init?.method === "PATCH" ? reply.promise : emptyRead(url));
    const { container } = mount(existing.id, [existing]);
    act(() => useSessionStore.setState({ connectorInstallations: [{
      id: "connector-1", label: "Connected source", kind: "github", auth_type: "oauth", scopes: [],
      external_account_id: "1", enable_by_default: false, needs_reconnect: false,
      token_expires_at: null, last_refresh_error_code: null, created_at: "2026-11-14",
    }] }));
    fireEvent.click(screen.getByRole("button", { name: "Choose an avatar" }));
    expect(screen.getByRole("dialog", { name: "Choose an avatar" })).toBeVisible();
    const save = screen.getByRole("button", { name: "Save Agent" });
    const archive = screen.getByRole("button", { name: "Archive agent" });
    act(() => {
      save.click();
      fireEvent.change(screen.getByLabelText("One-line role"), { target: { value: "Too late" } });
      screen.getByRole("button", { name: "Use 🦊" }).click();
      archive.click();
      save.click();
    });
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(1);
    expect(screen.getByLabelText("One-line role")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Choose an avatar" })).toHaveTextContent("🐙");
    for (const control of container.querySelectorAll(".page-body input, .page-body textarea, .page-body select, .page-body button")) {
      expect(control).toBeDisabled();
    }
    expect(archive).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
    archive.click();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/archive"))).toHaveLength(0);
    await act(async () => reply.resolve(json({ detail: "Cannot save now" }, 409)));
    expect(screen.getByRole("button", { name: "Save Agent" })).toBeEnabled();
    expect(screen.getByLabelText("One-line role")).toBeEnabled();
    expect(screen.getByLabelText("Name")).toHaveValue("Octo");
  }, 10_000);

  it.each([true, false])("lets a prior Connector write settle during Save without unlocking the editors (success=%s)", async success => {
    const connector = deferred<Response>(), save = deferred<Response>();
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/operations")) return json({ operation_id: "prior-write", reserved: true });
      if (init?.method === "PATCH") return url.includes("/connectors/") ? connector.promise : save.promise;
      return emptyRead(url);
    });
    mount("ag1");
    act(() => useSessionStore.setState({ connectorInstallations: [{
      id: "source", label: "Prior source", kind: "github", auth_type: "oauth", scopes: [], external_account_id: "1",
      enable_by_default: false, needs_reconnect: false, token_expires_at: null, last_refresh_error_code: null, created_at: "2026-11-14",
    }] }));
    await act(async () => {});
    const checkbox = screen.getByRole("checkbox", { name: /Prior source/ });
    await act(async () => fireEvent.click(checkbox));
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));
    await act(async () => connector.resolve(new Response(null, { status: success ? 204 : 503 })));
    expect(checkbox).toHaveProperty("checked", success);
    expect(checkbox).toBeDisabled();
    expect(screen.getByLabelText("System prompt", { exact: false })).toBeDisabled();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH")).toHaveLength(2);
    await act(async () => save.resolve(json({ detail: "Save refused" }, 409)));
    expect(checkbox).toHaveProperty("checked", success);
    if (success) expect(checkbox).toBeEnabled();
    else {
      expect(checkbox).toBeDisabled();
      expect(screen.getByRole("button", { name: "Check update for Prior source" })).toBeEnabled();
    }
  });

  it.each(["archive", "delete"])("an external %s keeps the draft's original edit target", async (change) => {
    const existing = agent({ is_system: false, name: "Original" });
    const writes: { url: string; method: string; body: unknown }[] = [];
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "PATCH" || init?.method === "POST") {
        writes.push({ url: String(url), method: init.method, body: JSON.parse(init.body as string) });
        return change === "archive" ? json({ ...existing, archived: true, description: "Saved draft" }) : json({ detail: "Agent not found" }, 404);
      }
      return emptyRead(url);
    });
    mount(existing.id, [existing]);
    fireEvent.change(screen.getByLabelText("One-line role"), { target: { value: "Saved draft" } });
    act(() => handleWsMessage(change === "archive"
      ? { type: "agent_archived", agent_id: existing.id, agent: { ...existing, archived: true } }
      : { type: "agent_deleted", agent_id: existing.id }));
    expect(screen.getByRole("button", { name: "Edit" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Create Agent" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));
    await waitFor(() => expect(writes).toEqual([{ url: `${window.location.origin}/api/agents/${existing.id}`, method: "PATCH", body: { description: "Saved draft" } }]));
    await waitFor(() => expect(screen.getByRole("button", { name: "Save Agent" })).toBeEnabled());
    expect(screen.getByLabelText("One-line role")).toHaveValue("Saved draft");
    expect(useSessionStore.getState().mainView).toBe("agent-form");
    expect(useSessionStore.getState().agents).toEqual([]);
    expect(useSessionStore.getState().archivedAgents.map((row) => row.id)).toEqual(change === "archive" ? [existing.id] : []);
  });

  it("keeps a restored receipt when an overlapping full snapshot arrives late", async () => {
    const retired = agent({ id: "retired", name: "Retired", is_system: false, archived: true });
    const read = deferred<Response>();
    let fullReads = 0;
    fetchMock.mockImplementation(async (url: unknown) => {
      if (String(url).includes("/unarchive")) return json({ ...retired, archived: false });
      if (String(url).includes("include_archived=true")) return ++fullReads === 1 ? json([retired]) : read.promise;
      return emptyRead(url);
    });
    const { container } = mount(null, [], "all");
    await screen.findByRole("button", { name: "Restore" });
    let loading!: Promise<void>;
    act(() => { loading = refreshAgents(undefined, "all"); });
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(useSessionStore.getState().agents).toHaveLength(1));
    await act(async () => { read.resolve(json([retired])); await loading; });
    expect(container.querySelectorAll(".library-name")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Restore" })).toBeNull();
    expect(screen.getByRole("button", { name: "All 1" })).toBeVisible();
    expect(useSessionStore.getState().archivedAgents).toEqual([]);
  });

  it("a newer live read preserves pending archive coverage and supersedes old live rows", async () => {
    const read = deferred<Response>();
    const retired = agent({ id: "retired", name: "Retired", archived: true, is_system: false });
    fetchMock.mockImplementation(async (url: unknown) => String(url).includes("include_archived=true") ? read.promise
      : json([agent({ name: "Current" })]));
    const { container } = mount(null, [agent()], "all");
    await act(async () => { await refreshAgents(); });
    await act(async () => { read.resolve(json([agent({ name: "Stale" }), agent({ id: "unseen", name: "Absent" }), retired])); });
    expect([...container.querySelectorAll(".library-name")].map((row) => row.textContent)).toEqual(["Current", "Retired"]);
    expect(screen.getByRole("button", { name: "Restore" })).toBeVisible();
    expect(screen.getByRole("button", { name: "All 2" })).toBeVisible();
  });

  it("a live absence leaves an older full read free to supply the same Agent's archive", async () => {
    const existing = agent({ is_system: false });
    const read = deferred<Response>();
    fetchMock.mockImplementation(async (url: unknown) => String(url).includes("include_archived=true") ? read.promise : json([]));
    const { container } = mount(null, [existing], "all");
    await act(async () => { await refreshAgents(); });
    await act(async () => read.resolve(json([{ ...existing, archived: true }])));
    expect(container.querySelectorAll(".library-name")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Restore" })).toBeVisible();
    expect(screen.getByRole("button", { name: "All 1" })).toBeVisible();
  });

  it("a late full-read failure cannot clear the next read's loading state", async () => {
    const first = deferred<Response>();
    const second = deferred<Response>();
    let reads = 0;
    fetchMock.mockImplementation(async () => ++reads === 1 ? first.promise : second.promise);
    mount(null, [], "all");
    let loading!: Promise<void>;
    act(() => { loading = refreshAgents(undefined, "all"); });
    await act(async () => first.resolve(json({}, 503)));
    expect(screen.getByRole("status")).toHaveTextContent("Loading all agents");
    expect(screen.queryByRole("alert")).toBeNull();
    await act(async () => { second.resolve(json([agent()])); await loading; });
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByRole("button", { name: "All 1" })).toBeVisible();
  });

  it("loads All lazily, retains partial archives on failure and confirms them on retry", async () => {
    const existing = agent({ is_system: false });
    let failing = true;
    fetchMock.mockImplementation(async (url: unknown) => String(url).includes("include_archived=true")
      ? failing ? json({}, 503) : json([{ ...existing, archived: true }]) : emptyRead(url));
    mount(existing.id, [existing]);
    await act(async () => {});
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("include_archived=true"))).toBe(false);
    act(() => handleWsMessage({ type: "agent_archived", agent_id: existing.id, agent: { ...existing, archived: true } }));
    expect(screen.getByRole("button", { name: "All 1+" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "All 1+" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: "Restore" })).toBeVisible();
    expect(screen.getByRole("button", { name: "All 1+" })).toBeVisible();
    failing = false;
    fireEvent.click(screen.getByRole("button", { name: "Refresh agents" }));
    await screen.findByRole("button", { name: "All 1" });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it.each(["success", "failure"])("retires an original account's full %s through account ABA", async (result) => {
    const read = deferred<Response>();
    let reads = 0;
    fetchMock.mockImplementation(async (url: unknown) => String(url).includes("include_archived=true")
      ? ++reads === 1 ? read.promise : json([agent({ name: "Current account" })]) : emptyRead(url));
    mount(null, [agent()], "all");
    act(() => {
      useSessionStore.getState().setToken("account-b");
      useSessionStore.getState().setToken("tok");
      useSessionStore.setState({ mainView: "agent-form", pageTab: "all" });
    });
    await screen.findByText("Current account");
    await act(async () => read.resolve(result === "success"
      ? json([agent({ id: "retired-account", archived: true })]) : json({}, 503)));
    expect(useSessionStore.getState().agents.map((row) => row.name)).toEqual(["Current account"]);
    expect(useSessionStore.getState().archivedAgents).toEqual([]);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it.each(["success", "failure"])("an old restore %s preserves the next draft while publishing qualified facts", async (result) => {
    const retired = agent({ id: "retired", name: "Retired", is_system: false, archived: true });
    const reply = deferred<Response>();
    fetchMock.mockImplementation(async (url: unknown) => String(url).endsWith("/unarchive") ? reply.promise
      : String(url).includes("include_archived=true") ? json([agent(), retired]) : emptyRead(url));
    mount(null, [agent()], "all");
    fireEvent.click(await screen.findByRole("button", { name: "Restore" }));
    act(() => useSessionStore.getState().openAgentForm());
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Next draft" } });
    await act(async () => reply.resolve(result === "success"
      ? json({ ...retired, archived: false }) : json({ detail: "Old restore failed" }, 503)));
    expect(screen.getByLabelText("Name")).toHaveValue("Next draft");
    expect(useSessionStore.getState().mainView).toBe("agent-form");
    expect(screen.queryByText("Old restore failed")).toBeNull();
    expect(useSessionStore.getState().agents.some((row) => row.id === retired.id)).toBe(result === "success");
  });

  it.each(["success", "failure"])("a pin %s and its original compensation preserve another Agent's archive", async (result) => {
    const first = agent({ is_system: false });
    const second = agent({ id: "second", name: "Second", is_system: false });
    const reply = deferred<Response>();
    fetchMock.mockImplementation(async (url: unknown) => String(url).endsWith("/unpin") ? reply.promise : emptyRead(url));
    const { container } = mount(null, [first, second], "all");
    await act(async () => {});
    fireEvent.click(screen.getByLabelText(`Unpin ${first.name} from the sidebar`));
    act(() => handleWsMessage({ type: "agent_archived", agent_id: second.id, agent: { ...second, archived: true } }));
    // The failed write's GET contains stale B, but also a real offline addition.
    fetchMock.mockImplementation(async () => json([first, second, agent({ id: "third", name: "Third" })]));
    await act(async () => reply.resolve(result === "success" ? json({ ...first, pinned: false }) : json({}, 503)));
    expect(useSessionStore.getState().agents.some((row) => row.id === second.id)).toBe(false);
    expect(useSessionStore.getState().archivedAgents.map((row) => row.id)).toEqual([second.id]);
    expect(container.querySelectorAll(".library-archived .library-name")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Restore" })).toBeVisible();
    if (result === "failure") expect(screen.getByText("Third")).toBeVisible();
  });

  it("a failed pin confirms an offline archive in the already requested All range", async () => {
    const existing = agent({ is_system: false });
    let offlineArchive = false;
    fetchMock.mockImplementation(async (url: unknown) => {
      if (String(url).endsWith("/unpin")) { offlineArchive = true; return json({}, 503); }
      if (String(url).includes("include_archived=true")) return json([{ ...existing, archived: offlineArchive }]);
      if (String(url).endsWith("/api/agents")) return json(offlineArchive ? [] : [existing]);
      return emptyRead(url);
    });
    mount(null, [existing], "all");
    await screen.findByRole("button", { name: "All 1" });
    fireEvent.click(screen.getByLabelText(`Unpin ${existing.name} from the sidebar`));
    await screen.findByRole("button", { name: "Restore" });
    expect(screen.getByRole("button", { name: "All 1" })).toBeVisible();
  });

  it.each([false, true])("a hard delete cannot be resurrected by an old full snapshot (archived=%s)", async (archived) => {
    const existing = agent({ is_system: false, archived });
    const read = deferred<Response>();
    fetchMock.mockImplementation(async (url: unknown) => String(url).includes("include_archived=true") ? read.promise : emptyRead(url));
    mount(null, archived ? [] : [existing], "all");
    act(() => {
      if (archived) handleWsMessage({ type: "agent_archived", agent_id: existing.id, agent: existing });
      handleWsMessage({ type: "agent_deleted", agent_id: existing.id });
    });
    await act(async () => read.resolve(json([existing])));
    expect(useSessionStore.getState().agents).toEqual([]);
    expect(useSessionStore.getState().archivedAgents).toEqual([]);
    expect(screen.queryByText(existing.name)).toBeNull();
  });

  it("keeps Restore when a complete external archive moves a live row", async () => {
    const existing = agent({ id: "other", name: "Other", is_system: false });
    const { container } = mount(null, [existing], "all");
    await act(async () => {});
    act(() => handleWsMessage({ type: "agent_archived", agent_id: existing.id,
      agent: { ...existing, archived: true, description: "Archived elsewhere" } }));
    expect(screen.getByRole("button", { name: "Restore" })).toBeVisible();
    expect(container.querySelectorAll(".library-name")).toHaveLength(1);
    expect(useSessionStore.getState().agents).toEqual([]);
    expect(screen.getByText("Archived elsewhere")).toBeVisible();
  });

  it("publishes an external restore once across All sections and its count", async () => {
    const retired = agent({ id: "retired", name: "Retired", is_system: false, archived: true });
    fetchMock.mockImplementation(async (url: unknown) => String(url).includes("include_archived=true")
      ? json([agent(), retired]) : emptyRead(url));
    const { container } = mount(null, [agent()], "all");
    await screen.findByRole("button", { name: "Restore" });
    act(() => handleWsMessage({ type: "agent_updated", agent: { ...retired, archived: false } }));
    expect(container.querySelectorAll(".library-name")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Restore" })).toBeNull();
    expect(screen.getByRole("button", { name: "All 2" })).toBeVisible();
  });

  it.each([
    { transition: "Agent switch", lateFailure: false },
    { transition: "A→B→A", lateFailure: true },
    { transition: "close and reopen", lateFailure: false },
    { transition: "account switch", lateFailure: true },
  ])("ignores a retired binding read after $transition (failed=$lateFailure)", async ({ transition, lateFailure }) => {
    const previous = deferred<Response>();
    const current = deferred<Response>();
    let reads = 0;
    fetchMock.mockImplementation(async (url: string) => {
      if (!url.endsWith("/connectors")) return emptyRead(url);
      if (transition === "A→B→A" && url.endsWith("/ag2/connectors")) return json({ installation_ids: ["github"] });
      return ++reads === 1 ? previous.promise : current.promise;
    });
    const agents = [agent(), agent({ id: "ag2", name: "Vera" })];
    const installation = {
      id: "github", kind: "github", label: "GitHub account", auth_type: "oauth" as const, scopes: [],
      external_account_id: "1", enable_by_default: false, needs_reconnect: false,
      token_expires_at: null, last_refresh_error_code: null, created_at: "2026-11-10",
    };
    mount("ag1", agents);
    act(() => useSessionStore.setState({ connectorInstallations: [installation] }));
    expect(reads).toBe(1);
    if (transition === "Agent switch" || transition === "A→B→A") {
      await act(async () => useSessionStore.getState().openAgentForm("ag2"));
      if (transition === "A→B→A") {
        expect(screen.getByRole("checkbox", { name: /GitHub account/ })).toBeChecked();
        act(() => useSessionStore.getState().openAgentForm("ag1"));
      }
    } else if (transition === "close and reopen") {
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
      act(() => useSessionStore.getState().openAgentForm("ag1"));
    } else {
      act(() => {
        useSessionStore.getState().setToken("other-account");
        useSessionStore.setState({ agents, connectorInstallations: [installation] });
        useSessionStore.getState().openAgentForm("ag1");
      });
    }
    expect(reads).toBe(2);
    const checkbox = screen.getByRole("checkbox", { name: /GitHub account/ });
    await act(async () => previous.resolve(lateFailure ? json({}, 503) : json({ installation_ids: ["github"] })));
    expect(screen.getByRole("status")).toHaveTextContent("Loading Connector bindings…");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(checkbox).toBeDisabled();
    await act(async () => current.resolve(json({ installation_ids: [] })));
    expect(checkbox).not.toBeChecked();
    expect(checkbox).toBeEnabled();
  });

  it("keeps an unconfirmed write locked until its own settlement confirms the current value", async () => {
    const connector = deferred<Response>();
    const settlement = deferred<Response>();
    let checks = 0;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/operations")) return json({ operation_id: "first-write", reserved: true });
      if (url.endsWith("/operations/first-write/settle")) return ++checks === 1 ? json({}, 503) : settlement.promise;
      if (init?.method === "PATCH") return connector.promise;
      return emptyRead(url);
    });
    mount("ag1");
    showConnectorInstallations("first", "second");
    const first = screen.getByRole("checkbox", { name: /first/ });
    const second = screen.getByRole("checkbox", { name: /second/ });
    await waitFor(() => expect(first).toBeEnabled());
    await act(async () => fireEvent.click(first));
    await act(async () => connector.resolve(new Response(null, { status: 503 })));
    const check = screen.getByRole("button", { name: "Check update for first" });
    expect(first).toBeDisabled();
    expect(second).toBeEnabled();
    expect(first).not.toBeChecked();
    await act(async () => fireEvent.click(check));
    expect(screen.getByText("Could not check update. Try again.")).toBeVisible();
    expect(first).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Check update for first" }));
    expect(first).toBeDisabled();
    await act(async () => settlement.resolve(json({ disposition: "applied", enabled: true })));
    expect(first).toBeChecked();
    expect(first).toBeEnabled();
  });

  it.each(["A→B→A", "new account", "reopen"] as const)("ignores a late settlement after %s", async (transition) => {
    const settlement = deferred<Response>();
    const agents = [agent(), agent({ id: "ag2", name: "Other agent" })];
    let opened = false;
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith("/operations/original/settle")) return settlement.promise;
      if (url.endsWith("/operations/current/settle")) return json({ disposition: "retired", enabled: false });
      if (url.endsWith("/ag1/connectors")) return json({
        installation_ids: [],
        pending_operations: { first: opened ? "current" : "original" },
      });
      return emptyRead(url);
    });
    mount("ag1", agents);
    showConnectorInstallations("first");
    const checkUpdate = () => screen.findByRole("button", { name: "Check update for first" });
    fireEvent.click(await checkUpdate());
    expect(screen.getByText("Checking update…")).toBeVisible();
    opened = true;
    act(() => {
      const store = useSessionStore.getState();
      if (transition === "A→B→A") store.openAgentForm("ag2");
      else if (transition === "reopen") store.showChat();
      else {
        store.setToken("replacement");
        useSessionStore.setState({ agents, connectorInstallations: store.connectorInstallations });
      }
      useSessionStore.getState().openAgentForm("ag1");
    });
    await checkUpdate();
    await act(async () => settlement.resolve(json({ disposition: "applied", enabled: true })));
    const first = screen.getByRole("checkbox", { name: /first/ });
    expect(first).toBeDisabled();
    expect(first).not.toBeChecked();
    await act(async () => fireEvent.click(await checkUpdate()));
    expect(first).toBeEnabled();
    expect(first).not.toBeChecked();
  });

  it("confines a late write receipt to its original opening", async () => {
    const write = deferred<Response>();
    const writes: string[] = [];
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/operations")) return json({ operation_id: "write", reserved: true });
      if (init?.method === "PATCH") {
        writes.push(url);
        if (writes.length === 1) return write.promise;
        return new Response(null, { status: 204 });
      }
      return url.endsWith("/connectors") ? json({ installation_ids: ["first"] }) : emptyRead(url);
    });
    mount("ag1");
    act(() => useSessionStore.setState({ connectorInstallations: ["first", "second"].map((id) => ({
      id, kind: "github", label: id, auth_type: "oauth", scopes: [], external_account_id: id,
      enable_by_default: false, needs_reconnect: false, token_expires_at: null,
      last_refresh_error_code: null, created_at: "2026-11-10",
    })) }));
    const first = screen.getByRole("checkbox", { name: /first/ });
    const second = screen.getByRole("checkbox", { name: /second/ });
    await waitFor(() => expect(first).toBeChecked());
    fireEvent.click(first);
    expect(first).toBeDisabled();
    expect(second).toBeEnabled();
    fireEvent.click(first);
    fireEvent.click(second);
    await waitFor(() => expect(writes).toHaveLength(2));
    expect(second).toBeChecked();
    act(() => {
      useSessionStore.getState().showChat();
      useSessionStore.getState().openAgentForm("ag1");
    });
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /first/ })).toBeChecked());
    await act(async () => write.resolve(new Response(null, { status: 503 })));
    // A new edit still belongs to the reopened draft.
    await act(async () => fireEvent.click(screen.getByRole("checkbox", { name: /first/ })));
    expect(screen.getByRole("checkbox", { name: /first/ })).not.toBeChecked();
    expect(writes).toHaveLength(3);
  });

  it("retries the same unsent edit after its reservation was accepted but the response was lost", async () => {
    let originalIntent: string | undefined;
    const patches = vi.fn(() => new Response(null, { status: 204 }));
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/operations")) {
        const intent = JSON.parse(init!.body as string).intent_id;
        if (!originalIntent) {
          originalIntent = intent;
          throw new TypeError("Response lost");
        }
        return json({ operation_id: "original-reservation", reserved: originalIntent === intent });
      }
      if (init?.method === "PATCH") return patches();
      return emptyRead(url);
    });
    mount("ag1");
    showConnectorInstallations("first");
    const checkbox = screen.getByRole("checkbox", { name: /first/ });
    await waitFor(() => expect(checkbox).toBeEnabled());
    await act(async () => fireEvent.click(checkbox));
    expect(screen.getByText("Update was not sent. Try again.")).toBeVisible();
    expect(checkbox).not.toBeChecked();
    expect(checkbox).toBeEnabled();
    expect(patches).not.toHaveBeenCalled();
    await act(async () => fireEvent.click(checkbox));
    expect(checkbox).toBeChecked();
    expect(patches).toHaveBeenCalledOnce();
  });

  it.each([false, true])("shows the current binding from a completed preparation without a write (enabled=%s)", async enabled => {
    let preparations = 0;
    const connectorWrites = vi.fn();
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/operations")) {
        if (++preparations === 1) throw new TypeError("Response lost");
        return json({
          operation_id: "completed", reserved: false,
          settlement: { disposition: "applied", enabled },
        });
      }
      if (init?.method === "PATCH") return connectorWrites();
      return emptyRead(url);
    });
    mount("ag1");
    showConnectorInstallations("first");
    const checkbox = screen.getByRole("checkbox", { name: /first/ });
    await waitFor(() => expect(checkbox).toBeEnabled());
    expect(checkbox).not.toBeChecked();
    await act(async () => fireEvent.click(checkbox));
    expect(checkbox).not.toBeChecked();
    await act(async () => fireEvent.click(checkbox));
    expect(screen.getByText("Previous update confirmed.")).toBeVisible();
    expect(checkbox).toHaveProperty("checked", enabled);
    expect(checkbox).toBeEnabled();
    expect(connectorWrites).not.toHaveBeenCalled();
  });

  it.each([true, false])("does not send a prepared write after Save takes the form (Save still pending=%s)", async (pending) => {
    const reserve = deferred<Response>(), save = deferred<Response>();
    const connectorWrites = vi.fn(() => new Response(null, { status: 204 }));
    const intents: string[] = [];
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/operations")) {
        intents.push(JSON.parse(init!.body as string).intent_id);
        return intents.length === 1 ? reserve.promise : json({ operation_id: "staged", reserved: intents[0] === intents[1] });
      }
      if (init?.method === "PATCH") {
        if (url.includes("/connectors/")) return connectorWrites();
        return save.promise;
      }
      return emptyRead(url);
    });
    mount("ag1");
    showConnectorInstallations("first");
    const checkbox = screen.getByRole("checkbox", { name: /first/ });
    await waitFor(() => expect(checkbox).toBeEnabled());
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));
    if (!pending) await act(async () => save.resolve(json({ detail: "Save refused" }, 409)));
    await act(async () => reserve.resolve(json({ operation_id: "staged", reserved: true })));
    if (pending) {
      expect(checkbox).toBeDisabled();
      await act(async () => save.resolve(json({ detail: "Save refused" }, 409)));
    }
    expect(checkbox).toBeEnabled();
    expect(checkbox).not.toBeChecked();
    expect(connectorWrites).not.toHaveBeenCalled();
    await act(async () => fireEvent.click(checkbox));
    expect(checkbox).toBeChecked();
    expect(connectorWrites).toHaveBeenCalledOnce();
  });

  it.each(["switch", "reopen", "account"] as const)(
    "a retired save cannot close, report errors in or unlock a new %s draft",
    async (transition) => {
      const existing = agent();
      const other = agent({ id: "ag2", name: "Vera" });
      const saves: (ReturnType<typeof deferred<Response>> & { body: unknown })[] = [];
      fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
        if (init?.method === "PATCH") {
          const save = { ...deferred<Response>(), body: JSON.parse(init.body as string) };
          saves.push(save);
          return save.promise;
        }
        return emptyRead(url);
      });
      mount("ag1", [existing, other]);
      fireEvent.change(screen.getByLabelText("One-line role"), { target: { value: "Old draft" } });
      fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));
      act(() => {
        if (transition === "account") {
          useSessionStore.getState().setToken("other-account");
          useSessionStore.getState().setToken("tok");
          useSessionStore.setState({ agents: [existing, other] });
        } else if (transition === "switch") useSessionStore.getState().openAgentForm("ag2");
        else useSessionStore.getState().showChat();
        useSessionStore.getState().openAgentForm("ag1");
      });
      expect(screen.getByRole("button", { name: "Save Agent" })).toBeEnabled();
      fireEvent.change(screen.getByLabelText("One-line role"), { target: { value: "New draft" } });
      fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));
      expect(saves.map((save) => save.body)).toEqual([{ description: "Old draft" }, { description: "New draft" }]);
      await act(async () => saves[0].resolve(transition === "reopen"
        ? json({ detail: "Retired save failed" }, 503)
        : json({ ...existing, description: "Old draft" })));
      expect(useSessionStore.getState().mainView).toBe("agent-form");
      expect(screen.getByLabelText("One-line role")).toHaveValue("New draft");
      expect(screen.queryByText("Retired save failed")).toBeNull();
      expect(screen.getByRole("button", { name: "Save Agent" })).toBeDisabled();
      if (transition === "switch") expect(useSessionStore.getState().agents[0].description).toBe("Old draft");
      if (transition === "account") expect(useSessionStore.getState().agents[0].description).toBe("");
      await act(async () => saves[1].resolve(json({ ...existing, description: "New draft" })));
      expect(useSessionStore.getState().mainView).toBe("chat");
    },
  );

  it.each([true, false])("admits Connector edits after confirming the initial binding (enabled=%s)", async enabled => {
    const read = deferred<Response>();
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/operations")) return json({ operation_id: "confirmed", reserved: true });
      if (init?.method === "PATCH") return new Response(null, { status: 204 });
      if (url.endsWith("/ag1/connectors")) return read.promise;
      return emptyRead(url);
    });
    mount("ag1");
    act(() => useSessionStore.setState({ connectorInstallations: [{
      id: "github", kind: "github", label: "GitHub account", auth_type: "oauth", scopes: [],
      external_account_id: "1", enable_by_default: false, needs_reconnect: false,
      token_expires_at: null, last_refresh_error_code: null, created_at: "2026-11-10",
    }] }));
    const checkbox = screen.getByRole("checkbox", { name: /GitHub account/ });
    expect(checkbox).toBeDisabled();
    expect(screen.getByText("Loading Connector bindings…")).toBeVisible();
    fireEvent.click(checkbox);
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(false);
    await act(async () => read.resolve(json({ installation_ids: enabled ? ["github"] : [] })));
    expect(screen.queryByText("Loading Connector bindings…")).toBeNull();
    expect(checkbox).toHaveProperty("checked", enabled);
    expect(checkbox).toBeEnabled();
    await act(async () => fireEvent.click(checkbox));
    const reservation = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/ag1/connectors/github/operations"));
    expect(reservation?.[1]?.method).toBe("POST");
    const intent = JSON.parse(reservation?.[1]?.body as string);
    expect(intent).toEqual({ enabled: !enabled, intent_id: expect.any(String) });
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/ag1/connectors/github"),
      expect.objectContaining({
        method: "PATCH", body: JSON.stringify({ operation_id: "confirmed", intent_id: intent.intent_id }),
      }));
  });

  it("retries a failed binding read in the same form after Save unfreezes it", async () => {
    const read = deferred<Response>();
    const retry = deferred<Response>();
    const save = deferred<Response>();
    let reads = 0;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/ag1/connectors")) return ++reads === 1 ? read.promise : retry.promise;
      if (init?.method === "PATCH") return save.promise;
      return emptyRead(url);
    });
    mount("ag1");
    act(() => useSessionStore.setState({ connectorInstallations: [{
      id: "github", kind: "github", label: "GitHub account", auth_type: "oauth", scopes: [],
      external_account_id: "1", enable_by_default: false, needs_reconnect: false,
      token_expires_at: null, last_refresh_error_code: null, created_at: "2026-11-10",
    }] }));
    const checkbox = screen.getByRole("checkbox", { name: /GitHub account/ });
    const role = screen.getByLabelText("One-line role");
    fireEvent.change(role, { target: { value: "Local draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));
    await act(async () => read.resolve(json({}, 503)));
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load Connector bindings.");
    const retryButton = screen.getByRole("button", { name: "Retry" });
    expect(retryButton).toBeDisabled();
    expect(checkbox).toBeDisabled();
    fireEvent.click(retryButton);
    expect(reads).toBe(1);
    await act(async () => save.resolve(json({ detail: "Save refused" }, 409)));
    expect(retryButton).toBeEnabled();
    fireEvent.click(retryButton);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Loading Connector bindings…");
    expect(checkbox).toBeDisabled();
    await act(async () => retry.resolve(json({ installation_ids: ["github"] })));
    expect(checkbox).toBeChecked();
    expect(checkbox).toBeEnabled();
    expect(role).toHaveValue("Local draft");
  });

  it.each(["update", "archive"])("a late form receipt preserves the newer WS %s", async (event) => {
    const existing = agent({ name: "Original" });
    let finish!: (response: Response) => void;
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (init?.method === "PATCH") return new Promise<Response>((resolve) => { finish = resolve; });
      return emptyRead(url);
    });
    mount(existing.id, [existing]);
    fireEvent.change(screen.getByLabelText("One-line role"), { target: { value: "Draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));
    await waitFor(() => expect(finish).toBeTypeOf("function"));
    act(() => handleWsMessage(event === "update"
      ? { type: "agent_updated", agent: { ...existing, name: "Newer update" } }
      : { type: "agent_archived", agent_id: existing.id }));
    await act(async () => finish(json({ ...existing, description: "Draft" })));
    const current = useSessionStore.getState().agents.find((row) => row.id === existing.id);
    if (event === "update") expect(current?.name).toBe("Newer update");
    else {
      expect(current).toBeUndefined();
      expect(useSessionStore.getState().mainView).toBe("agent-form");
    }
  });

  it("preserves a legacy unsupported policy and requires an explicit correction", async () => {
    const existing = agent({ backend: "codex", tool_allow: "Read", tool_deny: "Write" });
    mount(existing.id, [existing]);
    const allow = screen.getByLabelText(/Allowed tools/);
    const deny = screen.getByLabelText(/Denied tools/);
    expect(allow).toHaveValue("Read");
    expect(deny).toHaveValue("Write");
    expect(allow).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));
    expect(await screen.findByText(/cannot enforce tool_allow/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(false);
    expect(allow).toHaveValue("Read");
    fireEvent.change(allow, { target: { value: "" } });
    fireEvent.change(deny, { target: { value: "" } });
    expect(allow).toBeDisabled();
    expect(deny).toBeDisabled();
  });

  it("uses backend metadata for tool support without clearing values on an engine switch", () => {
    const existing = agent({ tool_deny: "Write" });
    mount(existing.id, [existing]);
    expect(screen.getByLabelText(/Allowed tools/)).toBeDisabled();
    expect(screen.getByLabelText(/Denied tools/)).toBeEnabled();
    fireEvent.click(screen.getByRole("radio", { name: /Codex/ }));
    expect(screen.getByLabelText(/Denied tools/)).toHaveValue("Write");
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));
    expect(screen.getByText(/cannot enforce tool_deny/)).toBeInTheDocument();
  });

  it("retains the last confirmed value on explicit refusal and removes deleted installations", async () => {
    let patches = 0;
    let removed = false;
    const installation = {
      id: "installation", kind: "github", label: "GitHub account", auth_type: "oauth" as const,
      external_account_id: "owner", scopes: [], enable_by_default: false, needs_reconnect: false,
      token_expires_at: null, last_refresh_error_code: null, created_at: "2026-10-19",
    };
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") { removed = true; return new Response(null, { status: 204 }); }
      if (url.endsWith("/api/connectors")) return json(removed ? [] : [installation]);
      if (url.endsWith("/connectors")) return json({ installation_ids: ["installation"] });
      if (url.endsWith("/operations")) return json({ operation_id: `write-${patches + 1}`, reserved: true });
      if (init?.method === "PATCH") {
        patches += 1;
        return patches === 1 ? json({ detail: "Connector installation not found" }, 404) : new Response(null, { status: 204 });
      }
      return emptyRead(url);
    });
    mount("ag1");
    await act(async () => { await connectorInstallations.refresh(); });
    const checkbox = await screen.findByRole("checkbox", { name: /GitHub account/ });
    await waitFor(() => expect(checkbox).toBeChecked());
    fireEvent.click(checkbox);
    await waitFor(() => expect(patches).toBe(1));
    await waitFor(() => expect(checkbox).toBeChecked());
    expect(screen.getByText("Update was rejected.")).toBeVisible();
    fireEvent.click(checkbox);
    await waitFor(() => expect(patches).toBe(2));
    await waitFor(() => expect(checkbox).not.toBeChecked());
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/api/agents/ag1/connectors/installation"),
      expect.objectContaining({ method: "PATCH", body: expect.stringContaining('"operation_id":"write-2"') }));
    await act(async () => { await connectorInstallations.disconnect("installation"); });
    expect(screen.queryByRole("checkbox", { name: /GitHub account/ })).toBeNull();
  });

  it.each(["codex", "claude-code"] as const)("saves %s model and reasoning defaults", async (backend) => {
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (String(url).includes(`/backends/${backend}/models`)) return json({
        default_model: "model-a", default_reasoning_effort: "max",
        models: [{ id: "model-a", name: "Model A", reasoning_efforts: ["low", "high", "max"] }],
        reasoning_efforts: ["low", "high", "max"],
        service_tiers: backend === "codex" ? ["default", "fast"] : [],
        default_service_tier: backend === "codex" ? "default" : null,
      });
      return init?.method === "POST" ? json(agent({ name: "Reasoner", backend }), 201) : emptyRead(url);
    });
    mount(null, [], "form", backend);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Reasoner" } });
    fireEvent.click(screen.getByText("Model defaults", { exact: true }));
    await waitFor(() => expect(screen.getByLabelText("Reasoning effort")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "model-a" } });
    fireEvent.change(screen.getByLabelText("Reasoning effort"), { target: { value: "high" } });
    if (backend === "codex") {
      expect(screen.getByLabelText("Speed")).toHaveDisplayValue("Normal (default)");
      fireEvent.change(screen.getByLabelText("Speed"), { target: { value: "fast" } });
    } else {
      expect(screen.queryByLabelText("Speed")).toBeNull();
    }
    fireEvent.click(screen.getByRole("button", { name: "Create Agent" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true));
    const request = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(request?.[1].body))).toMatchObject({
      model: "model-a", reasoning_effort: "high", backend, service_tier: backend === "codex" ? "fast" : null,
    });
  });

  it("creates an agent with the install's configured Codex default", async () => {
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) =>
      init?.method === "POST"
        ? json(agent({ id: "ag2", name: "Codex default", backend: "codex", is_system: false }), 201)
        : emptyRead(url)
    );
    mount(null, [agent({ backend: "codex" })], "form", "codex");
    expect(screen.getByRole("radio", { name: /Codex/ }).getAttribute("aria-checked")).toBe("true");
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Codex default" } });
    fireEvent.click(screen.getByRole("button", { name: "Create Agent" }));

    await waitFor(() => expect(useSessionStore.getState().mainView).toBe("chat"));
    const call = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(call![1].body)).toMatchObject({ backend: "codex", service_tier: null });
  });

  it("applies a late default without clearing the draft's name", () => {
    mount();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "My agent" } });
    act(() => useSessionStore.setState({ backendCatalog: { status: "ready", catalog: backendCatalogFixture(undefined, "codex") } }));
    expect(screen.getByRole("radio", { name: /Codex/ }).getAttribute("aria-checked")).toBe("true");
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("My agent");
  });

  it("preserves an explicit backend choice when the install default arrives", () => {
    mount();
    fireEvent.click(screen.getByRole("radio", { name: /Claude/ }));
    act(() => useSessionStore.setState({ backendCatalog: { status: "ready", catalog: backendCatalogFixture(undefined, "codex") } }));
    expect(screen.getByRole("radio", { name: /Claude/ }).getAttribute("aria-checked")).toBe("true");
  });

  it("POSTs a new agent and returns to chat", async () => {
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) =>
      init?.method === "POST"
        ? new Response(JSON.stringify(agent({ id: "ag2", name: "sre-ops", is_system: false })), {
            status: 201,
            headers: { "Content-Type": "application/json" },
          })
        : emptyRead(url)
    );
    mount(null);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "sre-ops" } });
    fireEvent.click(screen.getByRole("button", { name: "Create Agent" }));

    await waitFor(() => expect(useSessionStore.getState().mainView).toBe("chat"));
    expect(useSessionStore.getState().agents.some((a) => a.id === "ag2")).toBe(true);
  });

  it("seeds the form when editing and PATCHes on save", async () => {
    const existing = agent({ id: "ag9", name: "Vera", is_system: false, system_prompt: "review code" });
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) =>
      init?.method === "PATCH"
        ? new Response(JSON.stringify({ ...existing, description: "reviewer" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          })
        : emptyRead(url)
    );
    mount("ag9", [existing], "form", "codex");
    expect(screen.getByRole("radio", { name: /Claude/ }).getAttribute("aria-checked")).toBe("true");
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("Vera");
    expect((screen.getByLabelText(/System prompt/) as HTMLTextAreaElement).value).toBe(
      "review code"
    );

    fireEvent.change(screen.getByLabelText("One-line role"), {
      target: { value: "reviewer" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([u, i]) => String(u).endsWith("/ag9") && (i as RequestInit)?.method === "PATCH"
        )
      ).toBe(true)
    );
    expect(patchBody()).toEqual({ description: "reviewer" });
  });

  it("PATCHes only speed when changing Normal to Fast", async () => {
    const existing = agent({ backend: "codex", service_tier: "default" });
    mockCodexEdit(existing);
    mount(existing.id, [existing]);
    await waitFor(() => expect(screen.getByLabelText("Speed")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Speed"), { target: { value: "fast" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));

    await waitFor(() => expect(patchBody()).toEqual({ service_tier: "fast" }));
    expect(useSessionStore.getState().agents[0].service_tier).toBe("fast");
  });

  it("PATCHes only speed when explicitly changing Fast to Normal", async () => {
    const existing = agent({ backend: "codex", service_tier: "fast" });
    mockCodexEdit(existing);
    mount(existing.id, [existing]);
    await waitFor(() => expect(screen.getByLabelText("Speed")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Speed"), { target: { value: "default" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));

    await waitFor(() => expect(patchBody()).toEqual({ service_tier: "default" }));
  });

  it("preserves another client's Fast setting when only editing the description", async () => {
    const existing = agent({ backend: "codex", service_tier: "default" });
    mockCodexEdit(existing);
    mount(existing.id, [existing]);
    await waitFor(() => expect(screen.getByLabelText("Speed")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("One-line role"), { target: { value: "reviewer" } });
    act(() => useSessionStore.getState().upsertAgent({ ...existing, service_tier: "fast" }));
    expect(screen.getByLabelText("Speed")).toHaveDisplayValue("Normal");
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));

    await waitFor(() => expect(patchBody()).toEqual({ description: "reviewer" }));
    await waitFor(() => expect(useSessionStore.getState().mainView).toBe("chat"));
    expect(useSessionStore.getState().agents[0]).toMatchObject({ description: "reviewer", service_tier: "fast" });
  });

  it("clears explicit Normal so other engines inheriting this Agent can use their defaults", async () => {
    const existing = agent({ backend: "codex", service_tier: "default" });
    mockCodexEdit(existing);
    mount(existing.id, [existing]);
    await waitFor(() => expect(screen.getByLabelText("Speed")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Speed"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));
    await waitFor(() => expect(patchBody()).toEqual({ service_tier: null }));
  });

  it("keeps unchanged null bindings while displaying Normal", async () => {
    const existing = agent({
      backend: "codex", service_tier: null, reasoning_effort: null,
      credential_id: null, model: null, avatar: null,
    });
    mockCodexEdit(existing);
    mount(existing.id, [existing]);
    await waitFor(() => expect(screen.getByLabelText("Speed")).toBeEnabled());
    expect(screen.getByLabelText("Speed")).toHaveDisplayValue("Normal (default)");
    fireEvent.change(screen.getByLabelText("One-line role"), { target: { value: "reviewer" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));

    await waitFor(() => expect(patchBody()).toEqual({ description: "reviewer" }));
    await waitFor(() => expect(useSessionStore.getState().mainView).toBe("chat"));
    expect(useSessionStore.getState().agents[0]).toMatchObject({
      service_tier: null, reasoning_effort: null, credential_id: null, model: null, avatar: null,
    });
  });

  it("can set Fast when the opening speed binding is null", async () => {
    const existing = agent({ backend: "codex", service_tier: null });
    mockCodexEdit(existing);
    mount(existing.id, [existing]);
    await waitFor(() => expect(screen.getByLabelText("Speed")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Speed"), { target: { value: "fast" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));

    await waitFor(() => expect(patchBody()).toEqual({ service_tier: "fast" }));
  });

  it("resets engine settings when switching from Codex to Claude Code", async () => {
    const existing = agent({
      backend: "codex", model: "model-a", reasoning_effort: "high", service_tier: "fast",
    });
    mockCodexEdit(existing);
    mount(existing.id, [existing]);
    fireEvent.click(screen.getByRole("radio", { name: /Claude/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));

    await waitFor(() => expect(patchBody()).toEqual({
      backend: "claude-code", credential_id: null, model: null,
      reasoning_effort: null, service_tier: null,
    }));
  });

  it("uses Normal and clears engine bindings when switching to Codex", async () => {
    const existing = agent({ model: "claude-model" });
    mockCodexEdit(existing);
    mount(existing.id, [existing]);
    fireEvent.click(screen.getByRole("radio", { name: /Codex/ }));
    await waitFor(() => expect(screen.getByLabelText("Speed")).toBeEnabled());
    expect(screen.getByLabelText("Speed")).toHaveDisplayValue("Normal (default)");
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));

    await waitFor(() => expect(patchBody()).toEqual({
      backend: "codex", credential_id: null, model: null,
      reasoning_effort: null, service_tier: null,
    }));
  });

  it("preserves unrelated fields updated by another client", async () => {
    const existing = agent({
      backend: "codex", model: "model-a", reasoning_effort: "high", service_tier: "default",
      system_prompt: "original",
      subagents: [{ name: "reviewer", description: "review", prompt: "original", model: null, tools: [] }],
    });
    mockCodexEdit(existing);
    mount(existing.id, [existing]);
    await waitFor(() => expect(screen.getByLabelText("Speed")).toBeEnabled());
    act(() => useSessionStore.getState().upsertAgent({
      ...existing, model: "model-b", reasoning_effort: "low", system_prompt: "updated",
      credential_id: "new-credential",
      subagents: [{ name: "helper", description: "help", prompt: "updated", model: null, tools: [] }],
    }));
    fireEvent.change(screen.getByLabelText("Speed"), { target: { value: "fast" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));

    await waitFor(() => expect(patchBody()).toEqual({ service_tier: "fast" }));
    await waitFor(() => expect(useSessionStore.getState().mainView).toBe("chat"));
    expect(useSessionStore.getState().agents[0]).toMatchObject({
      model: "model-b", reasoning_effort: "low", system_prompt: "updated",
      credential_id: "new-credential",
      subagents: [{ name: "helper" }], service_tier: "fast",
    });
  });

  it("won't offer Archive on the protected system agent", () => {
    // Scoped by class, not by name: the Archived *tab* also says "Archive…".
    const { container } = mount("ag1", [agent()]);
    expect(container.querySelector(".btn-agent-archive")).toBeNull();
  });

  it("archives an ordinary agent and drops it from the sidebar", async () => {
    const vera = agent({ id: "ag9", name: "Vera", is_system: false });
    fetchMock.mockImplementation(async (url: unknown) => String(url).endsWith("/archive")
      ? json({ ...vera, archived: true }) : emptyRead(url));
    const { container } = mount("ag9", [vera]);
    fireEvent.click(container.querySelector(".btn-agent-archive")!);
    await waitFor(() =>
      expect(useSessionStore.getState().agents.some((a) => a.id === "ag9")).toBe(false)
    );
    expect(
      fetchMock.mock.calls.some(([u]) => String(u).endsWith("/ag9/archive"))
    ).toBe(true);
  });

  it("lists every agent on the All tab, by section", async () => {
    const retired = agent({ id: "ag8", name: "Retired", is_system: false, archived: true });
    fetchMock.mockImplementation(async (url: unknown) =>
      String(url).includes("include_archived=true")
        ? json([...useSessionStore.getState().agents, retired])
        : emptyRead(url)
    );
    const { container } = mount(null, [
      agent(),
      agent({ id: "ag2", name: "Vera", is_system: false, pinned: false, pin_order: 2 }),
      agent({ id: "ag3", name: "Scout", is_system: false, pin_order: 3 }),
    ], "all");

    await waitFor(() => expect(screen.getByText("Retired")).toBeTruthy());
    const names = (section: string) =>
      [...container.querySelectorAll(`.library-${section} .library-name`)].map(
        (n) => n.textContent
      );
    expect(names("pinned")).toEqual(["Octo", "Scout"]);
    expect(names("unpinned")).toEqual(["Vera"]);
    expect(names("archived")).toEqual(["Retired"]);
    // The tab counts live and archived together.
    expect(screen.getByText("All 4")).toBeTruthy();
  });

  it("filters by name and description", () => {
    const { container } = mount(null, [
      agent(),
      agent({ id: "ag2", name: "Vera", description: "code reviewer", is_system: false }),
    ], "all");
    fireEvent.change(screen.getByLabelText("Filter agents"), {
      target: { value: "review" },
    });
    expect(
      [...container.querySelectorAll(".library-name")].map((n) => n.textContent)
    ).toEqual(["Vera"]);
  });

  it("pins and unpins from the All tab; the Default Agent stays pinned", async () => {
    const vera = agent({ id: "ag2", name: "Vera", is_system: false, pinned: false });
    fetchMock.mockImplementation(async (url: unknown) =>
      String(url).endsWith("/ag2/pin")
        ? json({ ...vera, pinned: true, pin_order: 2 })
        : emptyRead(url)
    );
    mount(null, [agent(), vera], "all");

    expect(screen.getByLabelText("Octo is always in the sidebar")).toBeTruthy();
    expect(screen.queryByLabelText("Unpin Octo from the sidebar")).toBeNull();

    fireEvent.click(screen.getByLabelText("Pin Vera to the sidebar"));
    await waitFor(() =>
      expect(
        useSessionStore.getState().agents.find((a) => a.id === "ag2")?.pinned
      ).toBe(true)
    );
  });

  it("Chat opens the agent's latest session", async () => {
    const vera = agent({ id: "ag2", name: "Vera", is_system: false, pinned: false });
    mount(null, [agent(), vera], "all");
    useSessionStore.setState({
      sessions: [
        session({ id: "old", agent_id: "ag2", created_at: "2026-01-01T00:00:00Z" }),
        session({ id: "builder", agent_id: "ag2", origin: "application", created_at: "2026-02-01T00:00:00Z" }),
        // An application's conversation is not the user's chat.
        session({ id: "app", agent_id: "ag2", origin: "app", created_at: "2026-03-01T00:00:00Z" }),
        session({ id: "delegated", agent_id: "ag2", origin: "delegation", created_at: "2026-04-01T00:00:00Z" }),
      ],
    });
    fireEvent.click(
      screen.getAllByRole("button", { name: "Chat" })[1]
    );
    await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe("builder"));
    expect(useSessionStore.getState().mainView).toBe("chat");
  });

  it("Chat starts a session when the agent has none", async () => {
    const vera = agent({ id: "ag2", name: "Vera", is_system: false });
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) =>
      String(url).endsWith("/api/agents/ag2/sessions") && init?.method === "POST"
        ? json(session({ id: "fresh", agent_id: "ag2" }), 201)
        : emptyRead(url)
    );
    mount(null, [agent(), vera], "all");
    fireEvent.click(screen.getAllByRole("button", { name: "Chat" })[1]);
    await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe("fresh"));
    expect(useSessionStore.getState().sessions.some((s) => s.id === "fresh")).toBe(true);
  });

  it.each(["current", "view", "view-return", "tab", "tab-return", "edit", "edit-return", "opening"])(
    "keeps a created chat's data but limits navigation to its original All page: %s", async (transition) => {
    const receipt = deferred<Response>();
    const vera = agent({ id: "ag2", name: "Vera", is_system: false });
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) =>
      String(url).endsWith("/api/agents/ag2/sessions") && init?.method === "POST"
        ? receipt.promise : emptyRead(url));
    mount("ag1", [agent(), vera], "all");
    fireEvent.click(screen.getAllByRole("button", { name: "Chat" })[1]);
    act(() => {
      const store = useSessionStore.getState();
      if (transition.startsWith("view")) store.openManage("connectors");
      if (transition === "view-return") useSessionStore.setState({ mainView: "agent-form" });
      if (transition.startsWith("tab")) store.setPageTab("form");
      if (transition === "tab-return") store.setPageTab("all");
      if (transition.startsWith("edit")) useSessionStore.setState({ editingAgentId: "ag2" });
      if (transition === "edit-return") useSessionStore.setState({ editingAgentId: "ag1" });
      if (transition === "opening") { store.openAgentForm("ag1"); store.setPageTab("all"); }
    });
    const view = useSessionStore.getState().mainView;
    await act(async () => receipt.resolve(json(session({ id: "late-chat", agent_id: "ag2" }), 201)));
    await waitFor(() => expect(useSessionStore.getState().sessions.some((s) => s.id === "late-chat")).toBe(true));
    if (transition === "current") {
      expect(useSessionStore.getState().mainView).toBe("chat");
      expect(useSessionStore.getState().activeSessionId).toBe("late-chat");
    } else {
      expect(useSessionStore.getState().mainView).toBe(view);
      expect(useSessionStore.getState().activeSessionId).not.toBe("late-chat");
    }
  });

  it("shares one Chat submission across a double click and a tab round trip, then allows retry after rejection", async () => {
    const receipt = deferred<Response>();
    let writes = 0;
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) =>
      String(url).endsWith("/sessions") && init?.method === "POST"
        ? ++writes === 1 ? receipt.promise : json(session({ id: "retried" }), 201) : emptyRead(url));
    mount("ag1", [agent()], "all");
    const chat = screen.getByRole("button", { name: "Chat" });
    act(() => { fireEvent.click(chat); fireEvent.click(chat); });
    act(() => {
      useSessionStore.getState().setPageTab("form");
      useSessionStore.getState().setPageTab("all");
    });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Chat" })); });
    expect(writes).toBe(1);
    await act(async () => receipt.resolve(json({ detail: "Agent unavailable" }, 404)));
    expect(await screen.findByText(/HTTP 404|Agent unavailable/)).toBeVisible();
    expect(useSessionStore.getState().mainView).toBe("agent-form");
    fireEvent.click(screen.getByRole("button", { name: "Chat" }));
    await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe("retried"));
    expect(writes).toBe(2);
  });

  it.each(["A first", "B first", "A→B→A"] as const)("navigates only from the most recent Chat click: %s", async (order) => {
    const { reads, writes } = controlChatHttp();
    mount(null, [agent(), agent({ id: "ag2", name: "Vera", is_system: false, pinned: false })], "all");
    await clickChat("Octo");
    await clickChat("Vera");
    if (order === "A→B→A") await clickChat("Octo");
    expect(writes.map(({ agentId }) => agentId)).toEqual(["ag1", "ag2"]);
    const rows = [session({ id: "chat-a" }), session({ id: "chat-b", agent_id: "ag2" })];
    const first = order === "B first" ? 1 : 0;
    await act(async () => writes[first].reply.resolve(json(rows[first], 201)));
    expect(useSessionStore.getState().activeSessionId).toBe(order === "A first" ? null : rows[first].id);
    await act(async () => writes[1 - first].reply.resolve(json(rows[1 - first], 201)));
    expect(reads).toHaveLength(1);
    await act(async () => reads[0].reply.resolve(json(rows)));
    expect(useSessionStore.getState().activeSessionId).toBe(order === "A→B→A" ? "chat-a" : "chat-b");
    expect(useSessionStore.getState().sessions.map(({ id }) => id)).toEqual(["chat-a", "chat-b"]);
  });

  it.each([true, false])("lets only the latest Agent show a Chat error (older failure first: %s)", async (olderFirst) => {
    const { writes } = controlChatHttp();
    mount(null, [agent(), agent({ id: "ag2", name: "Vera", is_system: false, pinned: false })], "all");
    await clickChat("Octo");
    await clickChat("Vera");
    if (olderFirst) {
      await act(async () => writes[0].reply.resolve(json({}, 400)));
      expect(screen.queryByText("HTTP 400")).toBeNull();
    }
    await act(async () => writes[1].reply.resolve(json({}, 422)));
    if (!olderFirst) await act(async () => writes[0].reply.resolve(json({}, 400)));
    expect(screen.getByText("HTTP 422")).toBeVisible();
    expect(screen.queryByText("HTTP 400")).toBeNull();
    expect(useSessionStore.getState().mainView).toBe("agent-form");
  });

  it("retires an old Chat submission even after signing back into the same account", async () => {
    const { writes } = controlChatHttp();
    mount(null, [agent()], "all");
    await clickChat();
    act(() => {
      const store = useSessionStore.getState();
      store.setToken("another-account");
      store.setToken("tok");
      useSessionStore.setState({ agents: [agent()] });
      openAllAgents();
    });
    await clickChat();
    expect(writes.map(({ agentId }) => agentId)).toEqual(["ag1", "ag1"]);
    await act(async () => writes[0].reply.resolve(json(session({ id: "previous-sign-in" }), 201)));
    expect(useSessionStore.getState().sessions).toEqual([]);
    expect(useSessionStore.getState().mainView).toBe("agent-form");
    await act(async () => writes[1].reply.resolve(json(session({ id: "new-sign-in" }), 201)));
    expect(useSessionStore.getState().sessions.map(({ id }) => id)).toEqual(["new-sign-in"]);
    expect(useSessionStore.getState().activeSessionId).toBe("new-sign-in");
  });

  it("keeps an explicit sidebar creation independent from an unresolved All-page Chat", async () => {
    const { writes } = controlChatHttp();
    mount(null, [agent()], "all");
    render(<SidebarAgents />);
    await clickChat();
    fireEvent.click(screen.getByLabelText("New session for Octo"));
    fireEvent.change(screen.getByPlaceholderText("Session name"), { target: { value: "Explicit chat" } });
    fireEvent.click(screen.getByRole("button", { name: "Create session" }));
    expect(writes.map(({ agentId, body }) => [agentId, body.name])).toEqual([["ag1", undefined], ["ag1", "Explicit chat"]]);
    await act(async () => writes[1].reply.resolve(json(session({ id: "explicit" }), 201)));
    await act(async () => writes[0].reply.resolve(json({}, 400)));
    expect(useSessionStore.getState().activeSessionId).toBe("explicit");
  });

  it("refreshes once when transport fails and preserves its POST after another tab's chat comes and goes", async () => {
    const { reads, writes } = controlChatHttp();
    mount(null, [agent()], "all");
    await clickChat();
    await act(async () => writes[0].reply.reject(new Error("Connection lost")));
    expect(reads.map(({ archived }) => archived)).toEqual([false]);
    await clickChat();
    await clickChat();
    expect(reads).toHaveLength(1);
    await act(async () => reads[0].reply.resolve(json({}, 503)));
    expect(screen.getByText(/Chat creation could not be confirmed/)).toBeVisible();
    await clickChat();
    await act(async () => reads[1].reply.resolve(json([])));
    expect(useSessionStore.getState().activeSessionId).toBeNull();
    await clickChat();
    const fromAnotherTab = session({ id: "another-tab" });
    await act(async () => reads[2].reply.resolve(json([fromAnotherTab])));
    expect(useSessionStore.getState().activeSessionId).toBe(fromAnotherTab.id);
    await act(async () => {
      handleWsMessage({ type: "session_archived", old_session_id: fromAnotherTab.id, new_session_id: null });
      openAllAgents();
    });
    await clickChat();
    expect(reads).toHaveLength(4);
    await act(async () => reads[3].reply.resolve(json([])));
    expect(writes).toHaveLength(1);
  });

  it("opens a chat accepted from another tab while the unknown submission's list read is still pending", async () => {
    const { reads, writes } = controlChatHttp();
    mount(null, [agent()], "all");
    await clickChat();
    await act(async () => writes[0].reply.reject(new Error("Connection lost")));
    expect(reads).toHaveLength(1);
    const other = session({ id: "another-tab-restored" });
    await act(async () => handleWsMessage({ type: "session_unarchived", session_id: other.id, session: other }));
    expect(useSessionStore.getState().activeSessionId).toBe(other.id);
    await act(async () => reads[0].reply.resolve(json({}, 503)));
    await act(async () => {
      handleWsMessage({ type: "session_archived", old_session_id: other.id, new_session_id: null });
      openAllAgents();
    });
    await clickChat();
    expect(writes).toHaveLength(1);
    await act(async () => reads[1].reply.resolve(json([])));
  });

  it.each(["timed out decoding the body", "invalid 201", "server error"] as const)("keeps the POST when its response %s", async (outcome) => {
    vi.useFakeTimers();
    const { reads, writes } = controlChatHttp();
    mount(null, [agent()], "all");
    await clickChat();
    await act(async () => {
      if (outcome === "timed out decoding the body") {
        writes[0].reply.resolve(new Response(new ReadableStream(), { status: 201 }));
        await vi.advanceTimersByTimeAsync(30_000);
      } else {
        writes[0].reply.resolve(outcome === "invalid 201" ? json({ id: "not-a-session" }, 201) : json({}, 503));
      }
    });
    expect(reads.map(({ archived }) => archived)).toEqual([false]);
    await clickChat();
    expect(reads).toHaveLength(1);
    await act(async () => reads[0].reply.resolve(json([])));
    expect(screen.getByText(/Chat creation could not be confirmed/)).toBeVisible();
    expect(writes).toHaveLength(1);
  });

  it.each(["list first", "restore event first"] as const)("preserves the 201 ID and selection through a reconnect list gap: %s", async (order) => {
    vi.useFakeTimers();
    Socket.reset();
    vi.stubGlobal("WebSocket", Socket);
    const original = session({ id: "original-created-chat" });
    const { reads, writes, presences } = controlChatHttp(original.id);
    mount(null, [agent()], "all");
    renderHook(() => useWebSocket());
    const first = Socket.instances[0];
    await openSocket(first);
    await act(async () => reads[0].reply.resolve(json([])));
    await clickChat();
    const second = await reconnectSocket(first);
    await act(async () => reads[1].reply.resolve(json([])));
    act(() => {
      // The restore broadcast has yet to fill the gap in the list.
      showSession(original.id, original.agent_id);
      openAllAgents();
    });
    await act(async () => writes[0].reply.resolve(json(original, 201)));
    expect(reads.map(({ archived }) => archived)).toEqual([false, false, true]);
    await act(async () => reads[2].reply.resolve(json([])));
    expect(presences.map(({ id }) => id)).toEqual([original.id]);
    expect(useSessionStore.getState().activeSessionId).toBe(original.id);
    await act(async () => presences[0].reply.resolve(json({ archived: false, deletion_pending: false })));
    expect(useSessionStore.getState().activeSessionId).toBe(original.id);
    expect(useSessionStore.getState().sessions).toEqual([]);

    await clickChat();
    await clickChat();
    expect(writes).toHaveLength(1);
    expect(useSessionStore.getState().mainView).toBe("agent-form");
    expect(reads.map(({ archived }) => archived)).toEqual([false, false, true, true]);
    const restore = { type: "session_unarchived", session_id: original.id, session: original };
    if (order === "list first") await act(async () => reads[3].reply.resolve(json([original])));
    else await act(async () => sendSocket(second, restore));
    expect(useSessionStore.getState().mainView).toBe("chat");
    expect(useSessionStore.getState().activeSessionId).toBe(original.id);
    expect(writes).toHaveLength(1);
    if (order === "list first") await act(async () => sendSocket(second, restore));
    else {
      await act(async () => reads[3].reply.resolve(json([original])));
      await act(async () => reads[4].reply.resolve(json([original])));
    }
    expect(useSessionStore.getState().sessions.map(({ id }) => id)).toEqual([original.id]);
    expect(writes).toHaveLength(1);
  });

  it("waits for current list evidence when a late 201 matches a row retained from the old socket", async () => {
    vi.useFakeTimers();
    Socket.reset();
    vi.stubGlobal("WebSocket", Socket);
    const { reads, writes } = controlChatHttp();
    mount(null, [agent()], "all");
    renderHook(() => useWebSocket());
    const original = session({ id: "from-old-socket" });
    const first = Socket.instances[0];
    await openSocket(first);
    await act(async () => reads[0].reply.resolve(json([])));
    await clickChat();
    await act(async () => sendSocket(first, { type: "session_created", session_id: original.id, session: original }));
    expect(useSessionStore.getState().sessions).toEqual([original]);
    await reconnectSocket(first);
    await clickChat();
    expect(useSessionStore.getState().activeSessionId).toBeNull();
    await act(async () => writes[0].reply.resolve(json(original, 201)));
    await clickChat();
    expect(useSessionStore.getState().activeSessionId).toBeNull();
    expect(useSessionStore.getState().mainView).toBe("agent-form");
    expect(writes).toHaveLength(1);
    await act(async () => reads[1].reply.resolve(json([])));
    expect(reads[2].archived).toBe(true);
    await act(async () => reads[2].reply.resolve(json([{ ...original, archived: true }])));
    expect(useSessionStore.getState().activeSessionId).toBeNull();
    await clickChat();
    expect(writes).toHaveLength(2);
    await act(async () => writes[1].reply.resolve(json(session({ id: "after-archive" }), 201)));
    expect(useSessionStore.getState().activeSessionId).toBe("after-archive");
  });

  it("keeps the 201 ID occupied when an interim reconnect list shows it before inclusive confirmation", async () => {
    vi.useFakeTimers();
    Socket.reset();
    vi.stubGlobal("WebSocket", Socket);
    const original = session({ id: "interim-list-chat" });
    const { reads, writes, presences } = controlChatHttp(original.id);
    mount(null, [agent()], "all");
    renderHook(() => useWebSocket());
    const first = Socket.instances[0];
    await openSocket(first);
    await act(async () => reads[0].reply.resolve(json([])));
    await clickChat();
    const second = await reconnectSocket(first);
    await act(async () => writes[0].reply.resolve(json(original, 201)));
    expect(reads.map(({ archived }) => archived)).toEqual([false, false]);
    await act(async () => reads[1].reply.resolve(json([original])));
    expect(reads.map(({ archived }) => archived)).toEqual([false, false, true]);
    const interimNavigation = useSessionStore.getState().activeSessionId;
    act(openAllAgents);
    await clickChat();
    expect(useSessionStore.getState().activeSessionId).toBeNull();
    // The inclusive response misses the session because it was restored
    // between the live and archived reads. Its event has not arrived yet.
    await act(async () => reads[2].reply.resolve(json([])));
    expect(presences.map(({ id }) => id)).toEqual([original.id]);
    expect(useSessionStore.getState().sessions).toEqual([original]);
    await act(async () => presences[0].reply.resolve(json({ archived: false, deletion_pending: false })));
    await clickChat();
    expect(writes).toHaveLength(1);
    expect(useSessionStore.getState().activeSessionId).toBeNull();
    expect(interimNavigation).toBeNull();
    expect(reads.map(({ archived }) => archived)).toEqual([false, false, true, true]);
    await act(async () => reads[3].reply.resolve(json([original])));
    expect(useSessionStore.getState().activeSessionId).toBe(original.id);
    await act(async () => sendSocket(second, { type: "session_unarchived", session_id: original.id, session: original }));
  });

  it("opens a row accepted by the new socket after the unknown POST's reconnect read fails", async () => {
    vi.useFakeTimers();
    Socket.reset();
    vi.stubGlobal("WebSocket", Socket);
    const { reads, writes } = controlChatHttp();
    mount(null, [agent()], "all");
    renderHook(() => useWebSocket());
    const first = Socket.instances[0];
    await openSocket(first);
    await act(async () => reads[0].reply.resolve(json([])));
    await clickChat();
    await act(async () => writes[0].reply.reject(new Error("Connection lost")));
    await act(async () => reads[1].reply.resolve(json({}, 503)));
    const second = await reconnectSocket(first);
    const other = session({ id: "restored-on-new-socket" });
    await act(async () => sendSocket(second, { type: "session_unarchived", session_id: other.id, session: other }));
    await act(async () => reads[2].reply.resolve(json({}, 503)));
    await clickChat();
    expect(useSessionStore.getState().activeSessionId).toBe(other.id);
    expect(reads).toHaveLength(3);
    await act(async () => {
      sendSocket(second, { type: "session_archived", old_session_id: other.id, new_session_id: null });
      openAllAgents();
    });
    await clickChat();
    expect(writes).toHaveLength(1);
    await act(async () => reads[3].reply.resolve(json([])));
  });

  it.each(["restore", "settled list"] as const)("uses current evidence ahead of a late 201: %s", async (evidence) => {
    vi.useFakeTimers();
    Socket.reset();
    vi.stubGlobal("WebSocket", Socket);
    const { reads, writes } = controlChatHttp();
    mount(null, [agent()], "all");
    renderHook(() => useWebSocket());
    const first = Socket.instances[0];
    await openSocket(first);
    await act(async () => reads[0].reply.resolve(json([])));
    await clickChat();
    const second = await reconnectSocket(first);
    const original = session({ id: "restored-before-late-201" });
    if (evidence === "restore") {
      await act(async () => sendSocket(second, { type: "session_unarchived", session_id: original.id, session: original }));
    } else {
      await act(async () => reads[1].reply.resolve(json([original])));
    }
    await act(async () => writes[0].reply.resolve(json(original, 201)));
    expect(useSessionStore.getState().activeSessionId).toBe(original.id);
    if (evidence === "restore") await act(async () => reads[1].reply.resolve(json({}, 503)));
    expect(reads.map(({ archived }) => archived)).toEqual([false, false]);
    expect(writes).toHaveLength(1);
  });

  it("opens a current ordinary list row after an unrelated archive follow-up fails", async () => {
    vi.useFakeTimers();
    Socket.reset();
    vi.stubGlobal("WebSocket", Socket);
    const { reads, writes } = controlChatHttp();
    mount(null, [agent()], "all");
    renderHook(() => useWebSocket());
    const first = Socket.instances[0];
    await openSocket(first);
    await act(async () => reads[0].reply.resolve(json([session({ id: "unrelated-before-refresh", agent_id: "ag2" })])));
    await clickChat();
    await act(async () => writes[0].reply.reject(new Error("Connection lost")));
    const other = session({ id: "other-chat-in-ordinary-list" });
    await act(async () => reads[1].reply.resolve(json([other])));
    expect(reads.map(({ archived }) => archived)).toEqual([false, false, true]);
    expect(useSessionStore.getState().activeSessionId).toBe(other.id);
    act(openAllAgents);
    await act(async () => reads[2].reply.resolve(json({}, 503)));
    await clickChat();
    expect(useSessionStore.getState().activeSessionId).toBe(other.id);
    expect(reads).toHaveLength(3);
    await act(async () => {
      sendSocket(first, { type: "session_archived", old_session_id: other.id, new_session_id: null });
      openAllAgents();
    });
    await clickChat();
    expect(writes).toHaveLength(1);
    await act(async () => reads[3].reply.resolve(json([])));
  });

  it.each(["confirmed receipt", "unknown submission"] as const)("continues the latest Chat confirmation across a canceled read: %s", async (outcome) => {
    vi.useFakeTimers();
    Socket.reset();
    vi.stubGlobal("WebSocket", Socket);
    const { reads, writes } = controlChatHttp();
    mount(null, [agent()], "all");
    renderHook(() => useWebSocket());
    const original = session({ id: "after-reconnect" });
    const other = session({ id: "other-agent-chat", agent_id: "other-agent" });
    const first = Socket.instances[0];
    await openSocket(first);
    await act(async () => reads[0].reply.resolve(json([])));
    await clickChat();
    await act(async () => {
      sendSocket(first, { type: "session_created", session_id: other.id, session: other });
      if (outcome === "confirmed receipt") writes[0].reply.resolve(json(original, 201));
      else writes[0].reply.reject(new Error("Connection lost"));
    });
    expect(reads).toHaveLength(2);
    await reconnectSocket(first);
    expect(screen.queryByText(/Chat creation could not be confirmed/)).toBeNull();
    expect(useSessionStore.getState().activeSessionId).toBeNull();
    expect(reads).toHaveLength(3);
    await act(async () => reads[1].reply.resolve(json([other])));
    await act(async () => reads[2].reply.resolve(json([original, other])));
    expect(useSessionStore.getState().activeSessionId).toBe(original.id);
    expect(writes).toHaveLength(1);
  });

  it.each(["archived row", "archive event", "delete event", "delete before 201"] as const)("permits fresh creation after an explicit %s", async (evidence) => {
    const { reads, writes } = controlChatHttp();
    mount(null, [agent()], "all");
    await clickChat();
    const original = session({ id: "original" });
    const other = session({ id: "unrelated", agent_id: "other-agent" });
    const deleted = { type: "session_deleted", session_id: original.id };
    await act(async () => {
      handleWsMessage({ type: "session_created", session_id: other.id, session: other });
      if (evidence === "delete before 201") handleWsMessage(deleted);
      writes[0].reply.resolve(json(original, 201));
    });
    if (evidence !== "delete before 201") {
      expect(reads.map(({ archived }) => archived)).toEqual([true]);
      await act(async () => reads[0].reply.resolve(json([other])));
      await clickChat();
      expect(writes).toHaveLength(1);
      await act(async () => {
        if (evidence === "archive event") handleWsMessage({ type: "session_archived", old_session_id: original.id, new_session_id: null });
        if (evidence === "delete event") handleWsMessage(deleted);
        reads[1].reply.resolve(json(evidence === "archived row" ? [other, { ...original, archived: true }] : [other]));
      });
      if (reads[2]) await act(async () => reads[2].reply.resolve(json([other])));
    }
    expect(useSessionStore.getState().sessions.some(({ id }) => id === original.id)).toBe(false);
    expect(useSessionStore.getState().mainView).toBe("agent-form");
    await clickChat();
    expect(writes).toHaveLength(2);
    await act(async () => writes[1].reply.resolve(json(session({ id: "replacement" }), 201)));
    expect(useSessionStore.getState().activeSessionId).toBe("replacement");
  });

  it("does not reopen a late 201 after an observed archive and confirmed deletion", async () => {
    const original = session({ id: "archived-before-receipt" });
    const { reads, writes, presences } = controlChatHttp(original.id);
    mount(null, [agent()], "all");
    await clickChat();
    await act(async () => handleWsMessage({ type: "session_archived", old_session_id: "other-1", new_session_id: null }));
    await act(async () => reads[0].reply.resolve(json([{ ...original, archived: true }])));
    expect(useSessionStore.getState().archivedSessions.map(({ id }) => id)).toContain(original.id);
    await act(async () => handleWsMessage({ type: "session_archived", old_session_id: "other-2", new_session_id: null }));
    await act(async () => reads[1].reply.resolve(json([])));
    expect(useSessionStore.getState().archivedSessions.map(({ id }) => id)).toContain(original.id);
    expect(presences.map(({ id }) => id)).toEqual([original.id]);
    await act(async () => presences[0].reply.resolve(json({ archived: null, deletion_pending: false })));
    expect(useSessionStore.getState().archivedSessions).toEqual([]);
    await act(async () => writes[0].reply.resolve(json(original, 201)));
    expect(useSessionStore.getState().sessions).toEqual([]);
    expect(useSessionStore.getState().activeSessionId).toBeNull();
    await clickChat();
    expect(writes).toHaveLength(2);
    await act(async () => writes[1].reply.resolve(json(session({ id: "replacement-after-late-201" }), 201)));
    expect(useSessionStore.getState().activeSessionId).toBe("replacement-after-late-201");
  });

  it("archives from a row and keeps the page open", async () => {
    const vera = agent({ id: "ag2", name: "Vera", is_system: false });
    fetchMock.mockImplementation(async (url: unknown) => String(url).endsWith("/archive")
      ? json({ ...vera, archived: true }) : emptyRead(url));
    const { container } = mount(null, [agent(), vera], "all");
    fireEvent.click(screen.getByLabelText("Archive Vera"));
    await waitFor(() =>
      expect(useSessionStore.getState().agents.some((a) => a.id === "ag2")).toBe(false)
    );
    expect(useSessionStore.getState().mainView).toBe("agent-form");
    expect(container.querySelector(".library-archived")).toBeVisible();
    expect(screen.getByRole("button", { name: "Restore" })).toBeVisible();
    expect(screen.queryByLabelText("Archive Octo")).toBeNull();
  });

  it("restores an archived agent and it comes back pinned", async () => {
    const retired = agent({ id: "ag8", name: "Retired", is_system: false, archived: true });
    fetchMock.mockImplementation(async (url: unknown) => {
      if (String(url).includes("/unarchive"))
        return json({ ...retired, archived: false, pinned: true, pin_order: 2 });
      if (String(url).includes("include_archived=true")) return json([agent(), retired]);
      return emptyRead(url);
    });
    const { container } = mount(null, [agent()], "all");
    await waitFor(() => expect(screen.getByText("Retired")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() =>
      expect(useSessionStore.getState().agents.some((a) => a.id === "ag8")).toBe(true)
    );
    // It stays on the All tab and moves to "In sidebar".
    expect(useSessionStore.getState().pageTab).toBe("all");
    await waitFor(() =>
      expect(
        [...container.querySelectorAll(".library-pinned .library-name")].map(
          (n) => n.textContent
        )
      ).toEqual(["Octo", "Retired"])
    );
  });

  it("surfaces a name clash when restoring", async () => {
    const retired = agent({ id: "ag8", name: "Scout", is_system: false, archived: true });
    fetchMock.mockImplementation(async (url: unknown) => {
      if (String(url).includes("/unarchive"))
        return json({ detail: "An agent named 'Scout' already exists" }, 400);
      if (String(url).includes("include_archived=true")) return json([retired]);
      return emptyRead(url);
    });
    mount(null, [agent()], "all");
    await waitFor(() => expect(screen.getByText("Scout")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(screen.getByText(/already exists/)).toBeTruthy());
  });

  it("a late restore receipt preserves an archive event and the archived row", async () => {
    const retired = agent({ id: "ag8", name: "Retired", is_system: false, archived: true });
    let finish!: (response: Response) => void;
    fetchMock.mockImplementation(async (url: unknown) => {
      if (String(url).includes("/unarchive")) return new Promise<Response>((resolve) => { finish = resolve; });
      if (String(url).includes("include_archived=true")) return json([retired]);
      return emptyRead(url);
    });
    mount(null, [agent()], "all");
    fireEvent.click(await screen.findByRole("button", { name: "Restore" }));
    await waitFor(() => expect(finish).toBeTypeOf("function"));
    act(() => handleWsMessage({ type: "agent_archived", agent_id: retired.id }));
    await act(async () => finish(json({ ...retired, archived: false })));
    expect(useSessionStore.getState().agents.some((row) => row.id === retired.id)).toBe(false);
    expect(screen.getByRole("button", { name: "Restore" })).toBeVisible();
  });

  it("names the second tab Edit while an agent is open", () => {
    mount("ag1", [agent()]);
    expect(screen.getByRole("button", { name: "Edit" })).toBeTruthy();
    cleanup();
    mount(null, [agent()]);
    expect(screen.getByRole("button", { name: "Create" })).toBeTruthy();
  });
});

function patchBody(): Record<string, unknown> | undefined {
  const call = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
  return call ? JSON.parse(String(call[1].body)) : undefined;
}

function mockCodexEdit(existing: Agent) {
  fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
    if (String(url).includes("/backends/codex/models")) return json({
      default_model: "model-a", default_reasoning_effort: "high",
      models: [{
        id: "model-a", name: "Model A", reasoning_efforts: ["low", "high"],
        service_tiers: ["default", "fast"],
      }],
      reasoning_efforts: ["low", "high"], service_tiers: ["default", "fast"],
      default_service_tier: "default",
    });
    if (init?.method === "PATCH") {
      const current = useSessionStore.getState().agents.find((a) => a.id === existing.id);
      return json({ ...current, ...JSON.parse(String(init.body)) });
    }
    return emptyRead(url);
  });
}

function showConnectorInstallations(...ids: string[]) {
  act(() => useSessionStore.setState({ connectorInstallations: ids.map((id) => ({
    id, kind: "github", label: id, auth_type: "oauth", scopes: [], external_account_id: id,
    enable_by_default: false, needs_reconnect: false,
    token_expires_at: null, last_refresh_error_code: null, created_at: "2026-11-10",
  })) }));
}

function emptyRead(url: unknown): Response {
  if (String(url).endsWith("/api/backends")) return json(backendCatalogFixture());
  if (String(url).includes("include_archived=true")) {
    const state = useSessionStore.getState();
    return json([...state.agents, ...state.archivedAgents]);
  }
  return json(/\/api\/agents\/[^/]+\/connectors$/.test(String(url)) ? { installation_ids: [] } : []);
}

function controlChatHttp(presenceId?: string) {
  const writes: { agentId: string; body: Record<string, unknown>; reply: ReturnType<typeof deferred<Response>> }[] = [];
  const reads: { archived: boolean; reply: ReturnType<typeof deferred<Response>> }[] = [];
  const presences: { id: string; reply: ReturnType<typeof deferred<Response>> }[] = [];
  fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
    const address = new URL(String(url), window.location.origin);
    const agentId = /^\/api\/agents\/([^/]+)\/sessions$/.exec(address.pathname)?.[1];
    if (agentId && init?.method === "POST") {
      const reply = deferred<Response>();
      writes.push({ agentId, body: JSON.parse(String(init.body)), reply });
      return reply.promise;
    }
    if (address.pathname === "/api/sessions") {
      const reply = deferred<Response>();
      reads.push({ archived: address.searchParams.has("include_archived"), reply });
      return reply.promise;
    }
    if (presenceId && address.pathname === `/api/sessions/${encodeURIComponent(presenceId)}/presence`) {
      const reply = deferred<Response>();
      presences.push({ id: presenceId, reply });
      return reply.promise;
    }
    if (address.pathname === "/api/agents" && !address.searchParams.has("include_archived")) {
      return json(useSessionStore.getState().agents);
    }
    return emptyRead(url);
  });
  return { reads, writes, presences };
}

async function clickChat(name?: string) {
  const row = name && screen.getByText(name).closest<HTMLElement>(".library-row");
  const button = (row ? within(row) : screen).getByRole("button", { name: "Chat" });
  await act(async () => { fireEvent.click(button); });
}

function openAllAgents() {
  const state = useSessionStore.getState();
  state.openAgentForm();
  state.setPageTab("all");
}

async function openSocket(socket: Socket) {
  await act(async () => {
    socket.readyState = Socket.OPEN;
    socket.onopen?.();
  });
}

async function reconnectSocket(first: Socket): Promise<Socket> {
  act(() => {
    first.readyState = Socket.CLOSED;
    first.onclose?.({ code: 1013 });
    vi.advanceTimersByTime(3000);
  });
  const second = Socket.instances[1];
  await openSocket(second);
  return second;
}

function sendSocket(socket: Socket, event: object) {
  socket.onmessage?.({ data: JSON.stringify(event) });
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function session(o: Partial<SessionInfo> = {}): SessionInfo {
  return {
    id: "s1",
    name: "Session",
    working_dir: "/tmp",
    status: "idle",
    created_at: "2026-01-01T00:00:00Z",
    archived: false,
    backend: "codex",
    message_count: 0,
    transcript_integrity: "ok",
    can_steer: false,
    can_observe_context: false,
    can_compact: false,
    can_fork: false,
    fork_is_full_copy: false,
    origin: "user",
    agent_id: "ag1",
    ...o,
  };
}
