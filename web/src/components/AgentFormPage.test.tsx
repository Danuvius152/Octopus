/**
 * The Agents page — the Create/Edit form (create, edit, archive) and the All
 * tab (sidebar-pins.md §6): every agent by section, pin toggles, Chat, and
 * restore from the Archived section.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { AgentFormPage } from "./AgentFormPage";
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
    ...o,
  } as Agent;
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
    mainView: "agent-form",
  });
  return render(<AgentFormPage onToggleSidebar={() => {}} />);
}

beforeEach(() => {
  fetchMock = vi.fn(async () => new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("confirm", vi.fn(() => true));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AgentFormPage", () => {
  it("saves Codex model, reasoning and Fast defaults", async () => {
    fetchMock.mockImplementation(async (url: unknown, init?: RequestInit) => {
      if (String(url).includes("/backends/codex/models")) return json({
        default_model: "model-a", default_reasoning_effort: "max",
        models: [{ id: "model-a", name: "Model A", reasoning_efforts: ["low", "high", "max"] }],
        reasoning_efforts: ["low", "high", "max"],
        service_tiers: ["default", "fast"], default_service_tier: "default",
      });
      return init?.method === "POST" ? json(agent({ name: "Reasoner", backend: "codex" }), 201) : json([]);
    });
    mount(null, [], "form", "codex");
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Reasoner" } });
    await waitFor(() => expect(screen.getByLabelText("Reasoning effort")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "model-a" } });
    fireEvent.change(screen.getByLabelText("Reasoning effort"), { target: { value: "high" } });
    expect(screen.getByLabelText("Speed")).toHaveDisplayValue("Normal (default)");
    fireEvent.change(screen.getByLabelText("Speed"), { target: { value: "fast" } });
    fireEvent.click(screen.getByRole("button", { name: "Create Agent" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true));
    const request = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(request?.[1].body))).toMatchObject({
      model: "model-a", reasoning_effort: "high", backend: "codex", service_tier: "fast",
    });
  });

  it("creates an agent with the install's configured Codex default", async () => {
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) =>
      init?.method === "POST"
        ? json(agent({ id: "ag2", name: "Codex default", backend: "codex", is_system: false }), 201)
        : json([])
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
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) =>
      init?.method === "POST"
        ? new Response(JSON.stringify(agent({ id: "ag2", name: "sre-ops", is_system: false })), {
            status: 201,
            headers: { "Content-Type": "application/json" },
          })
        : new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );
    mount(null);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "sre-ops" } });
    fireEvent.click(screen.getByRole("button", { name: "Create Agent" }));

    await waitFor(() => expect(useSessionStore.getState().mainView).toBe("chat"));
    expect(useSessionStore.getState().agents.some((a) => a.id === "ag2")).toBe(true);
  });

  it("seeds the form when editing and PATCHes on save", async () => {
    const existing = agent({ id: "ag9", name: "Vera", is_system: false, system_prompt: "review code" });
    fetchMock.mockImplementation(async (_url: unknown, init?: RequestInit) =>
      init?.method === "PATCH"
        ? new Response(JSON.stringify({ ...existing, description: "reviewer" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          })
        : new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
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
      system_prompt: "original", tool_allow: "tool-a", tool_deny: "tool-b",
      subagents: [{ name: "reviewer", description: "review", prompt: "original", model: null, tools: [] }],
    });
    mockCodexEdit(existing);
    mount(existing.id, [existing]);
    await waitFor(() => expect(screen.getByLabelText("Speed")).toBeEnabled());
    act(() => useSessionStore.getState().upsertAgent({
      ...existing, model: "model-b", reasoning_effort: "low", system_prompt: "updated",
      tool_allow: "new-allow", tool_deny: "new-deny", credential_id: "new-credential",
      subagents: [{ name: "helper", description: "help", prompt: "updated", model: null, tools: [] }],
    }));
    fireEvent.change(screen.getByLabelText("Speed"), { target: { value: "fast" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Agent" }));

    await waitFor(() => expect(patchBody()).toEqual({ service_tier: "fast" }));
    await waitFor(() => expect(useSessionStore.getState().mainView).toBe("chat"));
    expect(useSessionStore.getState().agents[0]).toMatchObject({
      model: "model-b", reasoning_effort: "low", system_prompt: "updated",
      tool_allow: "new-allow", tool_deny: "new-deny", credential_id: "new-credential",
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
        ? json([agent(), retired])
        : json([])
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
        : json([])
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
        : json([])
    );
    mount(null, [agent(), vera], "all");
    fireEvent.click(screen.getAllByRole("button", { name: "Chat" })[1]);
    await waitFor(() => expect(useSessionStore.getState().activeSessionId).toBe("fresh"));
    expect(useSessionStore.getState().sessions.some((s) => s.id === "fresh")).toBe(true);
  });

  it("archives from a row and keeps the page open", async () => {
    const vera = agent({ id: "ag2", name: "Vera", is_system: false });
    const { container } = mount(null, [agent(), vera], "all");
    fireEvent.click(screen.getByLabelText("Archive Vera"));
    await waitFor(() =>
      expect(useSessionStore.getState().agents.some((a) => a.id === "ag2")).toBe(false)
    );
    expect(useSessionStore.getState().mainView).toBe("agent-form");
    expect(container.querySelector(".library-archived")).toBeNull(); // the mock archive is empty
    expect(screen.queryByLabelText("Archive Octo")).toBeNull();
  });

  it("restores an archived agent and it comes back pinned", async () => {
    const retired = agent({ id: "ag8", name: "Retired", is_system: false, archived: true });
    fetchMock.mockImplementation(async (url: unknown) => {
      if (String(url).includes("/unarchive"))
        return json({ ...retired, archived: false, pinned: true, pin_order: 2 });
      if (String(url).includes("include_archived=true")) return json([agent(), retired]);
      return json([]);
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
      return json([]);
    });
    mount(null, [agent()], "all");
    await waitFor(() => expect(screen.getByText("Scout")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(screen.getByText(/already exists/)).toBeTruthy());
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
    return json([]);
  });
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
