/**
 * The Agents page — the Create/Edit form (create, edit, archive) and the All
 * tab (sidebar-pins.md §6): every agent by section, pin toggles, Chat, and
 * restore from the Archived section.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { AgentFormPage } from "./AgentFormPage";
import { handleWsMessage } from "../hooks/useWebSocket";
import { connectorInstallations } from "../lib/connectorInstallations";
import { deferred } from "../test-support/deferred";
import { refreshAgents } from "../lib/refreshAgents";
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
    availableBackends: ["claude-code", "codex"],
    defaultBackend,
    backendToolPolicies: { "claude-code": { allow: false, deny: true }, codex: { allow: false, deny: false } },
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
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AgentFormPage", () => {
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

  it("keeps a reopened Agent's connector selection when the first opening's GET arrives late", async () => {
    const read = deferred<Response>();
    let reads = 0;
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith("/ag1/connectors")) {
        if (++reads === 1) return read.promise;
        return json({ installation_ids: [] });
      }
      return emptyRead(url);
    });
    mount("ag1", [agent(), agent({ id: "ag2", name: "Vera" })]);
    act(() => useSessionStore.setState({ connectorInstallations: [{
      id: "github", kind: "github", label: "GitHub account", auth_type: "oauth", scopes: [],
      external_account_id: "1", enable_by_default: false, needs_reconnect: false,
      token_expires_at: null, last_refresh_error_code: null, created_at: "2026-11-10",
    }] }));
    await waitFor(() => expect(reads).toBe(1));
    act(() => useSessionStore.getState().openAgentForm("ag2"));
    act(() => useSessionStore.getState().openAgentForm("ag1"));
    await waitFor(() => expect(reads).toBe(2));
    expect(screen.getByRole("checkbox", { name: /GitHub account/ })).not.toBeChecked();
    await act(async () => read.resolve(json({ installation_ids: ["github"] })));
    expect(screen.getByRole("checkbox", { name: /GitHub account/ })).not.toBeChecked();
  });

  it("locks only a pending connector and confines its failed rollback to its original opening", async () => {
    const write = deferred<Response>();
    const writes: string[] = [];
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
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
    fireEvent.click(screen.getByRole("checkbox", { name: /first/ }));
    expect(screen.getByRole("checkbox", { name: /first/ })).not.toBeChecked();
    expect(writes).toHaveLength(3);
    await act(async () => {});
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

  it("keeps a connector edit made before its initial GET completes", async () => {
    const read = deferred<Response>();
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
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
    fireEvent.click(screen.getByRole("checkbox", { name: /GitHub account/ }));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /GitHub account/ })).toBeEnabled());
    await act(async () => read.resolve(json({ installation_ids: [] })));
    expect(screen.getByRole("checkbox", { name: /GitHub account/ })).toBeChecked();
  });

  it.each(["before", "after"])("a failed connector edit restores the initial selection read %s rejection", async (order) => {
    const read = deferred<Response>();
    const write = deferred<Response>();
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "PATCH") return write.promise;
      if (url.endsWith("/ag1/connectors")) return read.promise;
      return emptyRead(url);
    });
    mount("ag1");
    act(() => useSessionStore.setState({ connectorInstallations: [{
      id: "github", kind: "github", label: "GitHub account", auth_type: "oauth", scopes: [],
      external_account_id: "1", enable_by_default: false, needs_reconnect: false,
      token_expires_at: null, last_refresh_error_code: null, created_at: "2026-11-10",
    }] }));
    fireEvent.click(screen.getByRole("checkbox", { name: /GitHub account/ }));
    if (order === "before") await act(async () => read.resolve(json({ installation_ids: ["github"] })));
    await act(async () => write.resolve(new Response(null, { status: 503 })));
    if (order === "after") await act(async () => read.resolve(json({ installation_ids: ["github"] })));
    expect(screen.getByRole("checkbox", { name: /GitHub account/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /GitHub account/ })).toBeEnabled();
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

  it("loads connector enablement, rolls back a failed toggle and removes deleted installations", async () => {
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
      if (init?.method === "PATCH") {
        patches += 1;
        return new Response(null, { status: patches === 1 ? 500 : 204 });
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
    fireEvent.click(checkbox);
    await waitFor(() => expect(patches).toBe(2));
    await waitFor(() => expect(checkbox).not.toBeChecked());
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/api/agents/ag1/connectors/installation"),
      expect.objectContaining({ method: "PATCH", body: JSON.stringify({ enabled: false }) }));
    await act(async () => { await connectorInstallations.disconnect("installation"); });
    expect(screen.queryByRole("checkbox", { name: /GitHub account/ })).toBeNull();
  });

  it.each(["codex", "claude-code"] as const)("saves %s model and reasoning defaults", async (backend) => {
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (String(url).includes(`/backends/${backend}/models`)) return json({
        default_model: "model-a", default_reasoning_effort: "max",
        models: [{ id: "model-a", name: "Model A", reasoning_efforts: ["low", "high", "max"] }],
        reasoning_efforts: ["low", "high", "max"],
        service_tiers: ["default", "fast"], default_service_tier: "default",
      });
      return init?.method === "POST" ? json(agent({ name: "Reasoner", backend }), 201) : emptyRead(url);
    });
    mount(null, [], "form", backend);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Reasoner" } });
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
    expect(JSON.parse(call![1].body)).toMatchObject({ backend: "codex", service_tier: "default" });
  });

  it("applies a late default without clearing the draft's name", () => {
    mount();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "My agent" } });
    act(() => useSessionStore.setState({ defaultBackend: "codex" }));
    expect(screen.getByRole("radio", { name: /Codex/ }).getAttribute("aria-checked")).toBe("true");
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("My agent");
  });

  it("preserves an explicit backend choice when the install default arrives", () => {
    mount();
    fireEvent.click(screen.getByRole("radio", { name: /Claude/ }));
    act(() => useSessionStore.setState({ defaultBackend: "codex" }));
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
    expect(screen.getByLabelText("Speed")).toHaveDisplayValue("Normal (default)");
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));

    await waitFor(() => expect(patchBody()).toEqual({ description: "reviewer" }));
    await waitFor(() => expect(useSessionStore.getState().mainView).toBe("chat"));
    expect(useSessionStore.getState().agents[0]).toMatchObject({ description: "reviewer", service_tier: "fast" });
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
      reasoning_effort: null, service_tier: "default",
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
        session({ id: "new", agent_id: "ag2", created_at: "2026-02-01T00:00:00Z" }),
        // An application's conversation is not the user's chat.
        session({ id: "app", agent_id: "ag2", origin: "app", created_at: "2026-03-01T00:00:00Z" }),
      ],
    });
    fireEvent.click(
      screen.getAllByRole("button", { name: "Chat" })[1]
    );
    await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe("new"));
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

function emptyRead(url: unknown): Response {
  if (String(url).includes("include_archived=true")) {
    const state = useSessionStore.getState();
    return json([...state.agents, ...state.archivedAgents]);
  }
  return json(/\/api\/agents\/[^/]+\/connectors$/.test(String(url)) ? { installation_ids: [] } : []);
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
    origin: "user",
    agent_id: "ag1",
    ...o,
  } as SessionInfo;
}
