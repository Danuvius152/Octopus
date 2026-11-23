import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AgentFormPage } from "./AgentFormPage";
import { SidebarAgents } from "./SidebarAgents";
import { handleWsMessage } from "../hooks/useWebSocket";
import { useSessionStore, type Agent, type CredentialInfo } from "../stores/sessionStore";
import { backendCatalogFixture } from "../test-support/backendCatalog";
import { deferred } from "../test-support/deferred";

const agent: Agent = {
  id: "agent-1", name: "Octo", description: "", avatar: null, system_prompt: "",
  model: "saved-model", credential_id: null, backend: "codex", mcp_servers: [],
  tool_allow: "", tool_deny: "", is_system: true, archived: false, pinned: true,
  pin_order: 1, created_at: "2026-01-01", updated_at: "2026-01-01",
  active_session_count: 0, subagents: [],
};

const claudeCredential: CredentialInfo = {
  id: "claude-sign-in", label: "Claude sign-in", backend: "claude-code",
  auth_type: "oauth", created_at: "2026-01-01", status: "active", needs_reconnect: false,
};
const codexCredential: CredentialInfo = { ...claudeCredential, id: "codex-sign-in", label: "Codex sign-in", backend: "codex" };

const catalog = (available: string[], preferred = "claude-code") => Response.json(backendCatalogFixture(available, preferred));

function mockApi(readCatalog: (init?: RequestInit) => Promise<Response>, existing = agent) {
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const { pathname } = new URL(String(input), window.location.origin);
    if (pathname === "/api/backends") return readCatalog(init);
    if (pathname === "/api/agents" && init?.method === "POST") {
      return Response.json({ ...agent, ...JSON.parse(String(init.body)), id: "created" }, { status: 201 });
    }
    if (pathname === "/api/agents") return Response.json([existing]);
    if (pathname.endsWith("/models")) return Response.json({ models: [] });
    if (pathname.endsWith("/connectors")) return Response.json({ installation_ids: [] });
    if (pathname.endsWith("/sessions") && init?.method === "POST") return new Response(null, { status: 400 });
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

async function openSessionSettings() {
  fireEvent.click(await screen.findByRole("button", { name: "New session for Octo" }));
  const form = within(screen.getByRole("group", { name: "New session" }));
  fireEvent.click(form.getByRole("button", { name: /working dir/ }));
  return form;
}

beforeEach(() => {
  const { accountGeneration } = useSessionStore.getState();
  useSessionStore.setState({ ...useSessionStore.getInitialState(), token: "first", accountGeneration: accountGeneration + 1 });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.removeItem("octopus_token");
});

it("rejects a partial catalog, then lets an Agent use a supported engine with no CLI detected", async () => {
  const first = deferred<Response>();
  const retry = deferred<Response>();
  const read = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(retry.promise);
  const fetch = mockApi(read);
  useSessionStore.getState().openAgentForm();
  render(<><SidebarAgents /><AgentFormPage onToggleSidebar={() => {}} /></>);

  expect(screen.getByText("Loading engine catalog…")).toBeVisible();
  expect(screen.getByText("These switches select Octopus connectors.")).toBeVisible();
  expect(screen.getByText("These definitions are saved with the agent.")).toBeVisible();
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Prepared Agent" } });
  expect(screen.getByRole("button", { name: "Create Agent" })).toBeDisabled();
  await act(async () => first.resolve(Response.json({ available: ["codex"], default: "codex" })));
  expect(screen.getByText("Could not load engine catalog.")).toBeVisible();
  expect(screen.getByText("These switches select Octopus connectors.")).toBeVisible();
  expect(screen.getByText("These definitions are saved with the agent.")).toBeVisible();
  expect(screen.getByRole("button", { name: "Create Agent" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Retry engine catalog" }));
  expect(read).toHaveBeenCalledTimes(2);
  expect(screen.getByText("Loading engine catalog…")).toBeVisible();

  await act(async () => retry.resolve(catalog([], "codex")));
  expect(screen.getByText("No engine CLIs detected on this host.")).toBeVisible();
  expect(screen.getByRole("radio", { name: /Codex/ })).toBeChecked();
  expect(screen.getByLabelText("Name")).toHaveValue("Prepared Agent");
  fireEvent.click(screen.getByRole("radio", { name: /Claude Code/ }));
  fireEvent.click(screen.getByRole("button", { name: "Create Agent" }));
  await waitFor(() => expect(useSessionStore.getState().mainView).toBe("chat"));
  const post = fetch.mock.calls.find(([, init]) => init?.method === "POST");
  expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({ name: "Prepared Agent", backend: "claude-code" });

  const session = await openSessionSettings();
  expect(session.getByText("No engine CLIs detected on this host.")).toBeVisible();
  expect(session.getByRole("option", { name: /Agent default \(Codex.*CLI not detected/ })).toBeVisible();
  expect(session.getByLabelText("Engine")).toHaveValue("");
});

it("keeps the saved backend and model when an Agent opens before discovery finishes", async () => {
  const pending = deferred<Response>();
  mockApi(() => pending.promise);
  useSessionStore.setState({ agents: [{ ...agent, credential_id: "saved-sign-in" }] });
  useSessionStore.getState().openAgentForm(agent.id);
  render(<AgentFormPage onToggleSidebar={() => {}} />);

  expect(screen.getByRole("radio", { name: /Codex/ })).toBeChecked();
  expect(screen.getByLabelText("Model")).toHaveValue("saved-model");
  expect(screen.getByLabelText("Credential")).toHaveValue("saved-sign-in");
  await act(async () => pending.resolve(catalog(["claude-code"])));
  expect(screen.getByRole("radio", { name: /Codex.*CLI not detected/ })).toBeChecked();
  expect(screen.getByLabelText("Model")).toHaveValue("saved-model");
  expect(screen.getByLabelText("Credential")).toHaveValue("saved-sign-in");
});

it("keeps inheritance available during failure and sends only a discovered explicit override", async () => {
  const pending = deferred<Response>();
  const retry = deferred<Response>();
  const fetch = mockApi(vi.fn().mockReturnValueOnce(pending.promise).mockReturnValueOnce(retry.promise));
  render(<SidebarAgents />);
  const form = await openSessionSettings();
  const engine = form.getByLabelText("Engine");
  expect(engine).toHaveValue("");
  expect(form.getByRole("option", { name: "Agent default (Codex)" })).toBeVisible();
  expect(form.getByText("Loading engine catalog…")).toBeVisible();
  await act(async () => pending.resolve(new Response(null, { status: 503 })));
  expect(form.getByText("Could not load engine catalog.")).toBeVisible();
  fireEvent.click(form.getByRole("button", { name: "Create session" }));
  await waitFor(() => expect(form.getByRole("button", { name: "Create session" })).toBeEnabled());
  fireEvent.click(form.getByRole("button", { name: "Retry engine catalog" }));
  await act(async () => retry.resolve(catalog(["claude-code"])));
  expect(form.getByRole("option", { name: /Agent default \(Codex.*CLI not detected/ })).toBeVisible();
  expect(form.getAllByRole("option")).toHaveLength(2);
  expect(engine).toHaveValue("");
  fireEvent.change(engine, { target: { value: "claude-code" } });
  fireEvent.click(form.getByRole("button", { name: "Create session" }));
  const posts = fetch.mock.calls.filter(([, init]) => init?.method === "POST");
  expect(posts.map(([, init]) => JSON.parse(String(init?.body)).backend)).toEqual([null, "claude-code"]);
});

it("ignores a late catalog body from a different account in the new session form", async () => {
  const staleBody = deferred<unknown>();
  const stale = new Response();
  vi.spyOn(stale, "json").mockReturnValue(staleBody.promise);
  mockApi(async (init) => new Headers(init?.headers).get("Authorization") === "Bearer first"
    ? stale : catalog(["codex"], "codex"));
  render(<SidebarAgents />);
  const first = await openSessionSettings();
  expect(first.getByText("Loading engine catalog…")).toBeVisible();
  await waitFor(() => expect(stale.json).toHaveBeenCalledOnce());

  act(() => useSessionStore.getState().setToken("second"));
  const second = await openSessionSettings();
  await second.findByRole("option", { name: "Codex" });
  fireEvent.change(second.getByLabelText("Engine"), { target: { value: "codex" } });
  const oldCatalog = await catalog(["claude-code"]).json();
  await act(async () => staleBody.resolve(oldCatalog));
  expect(second.queryByRole("option", { name: "Claude Code" })).toBeNull();
  expect(second.getByLabelText("Engine")).toHaveValue("codex");
});

it("requires a compatible credential when the discovered engine cannot use the Agent credential", async () => {
  const fetch = mockApi(async () => catalog(["claude-code"]), { ...agent, credential_id: "codex-sign-in" });
  useSessionStore.setState({ credentials: [claudeCredential] });
  render(<SidebarAgents />);
  const form = await openSessionSettings();
  const create = form.getByRole("button", { name: "Create session" });
  expect(create).toBeEnabled();
  await form.findByRole("option", { name: "Claude Code" });
  fireEvent.change(form.getByLabelText("Engine"), { target: { value: "claude-code" } });
  expect(form.getByRole("option", { name: /Agent credential.*requires Codex/ })).toBeDisabled();
  expect(create).toBeDisabled();
  fireEvent.keyDown(form.getByPlaceholderText("Session name"), { key: "Enter" });
  expect(fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);

  fireEvent.change(form.getByLabelText("Credential"), { target: { value: "claude-sign-in" } });
  expect(create).toBeEnabled();
  fireEvent.click(create);
  const post = fetch.mock.calls.find(([, init]) => init?.method === "POST");
  expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({ backend: "claude-code", credential_id: "claude-sign-in" });
  await waitFor(() => expect(create).toBeEnabled());
});

it.each([
  { from: "", to: "codex" },
  { from: "codex", to: "" },
])("keeps a session credential when switching between inherited and explicit Codex ($from → $to)", async ({ from, to }) => {
  const fetch = mockApi(async () => catalog(["codex"]));
  useSessionStore.setState({ credentials: [codexCredential] });
  render(<SidebarAgents />);
  const form = await openSessionSettings();
  await form.findByRole("option", { name: "Codex" });
  const engine = form.getByLabelText("Engine");
  if (from) fireEvent.change(engine, { target: { value: from } });
  const credential = form.getByLabelText("Credential");
  fireEvent.change(credential, { target: { value: codexCredential.id } });

  fireEvent.change(engine, { target: { value: to } });
  expect(credential).toHaveValue(codexCredential.id);
  const create = form.getByRole("button", { name: "Create session" });
  fireEvent.click(create);
  const post = fetch.mock.calls.find(([, init]) => init?.method === "POST");
  expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({ backend: to || null, credential_id: codexCredential.id });
  await waitFor(() => expect(create).toBeEnabled());
});

it.each(["credential", "engine"] as const)("keeps a conflicting session credential visible until the user changes the %s", async (field) => {
  const fetch = mockApi(async () => catalog(["claude-code", "codex"]), { ...agent, backend: "claude-code" });
  useSessionStore.setState({ credentials: [claudeCredential, codexCredential] });
  render(<SidebarAgents />);
  const form = await openSessionSettings();
  await form.findByRole("option", { name: "Codex" });
  const credential = form.getByLabelText("Credential");
  fireEvent.change(credential, { target: { value: claudeCredential.id } });

  act(() => handleWsMessage({ type: "agent_updated", agent }));
  expect(form.getByRole("option", { name: "Agent default (Codex)" })).toBeVisible();
  expect(form.getByRole("option", { name: /Claude sign-in.*requires Claude Code/ })).toBeDisabled();
  expect(credential).toHaveValue(claudeCredential.id);
  const create = form.getByRole("button", { name: "Create session" });
  expect(create).toBeDisabled();
  fireEvent.keyDown(form.getByPlaceholderText("Session name"), { key: "Enter" });
  expect(fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);

  const backend = field === "engine" ? "claude-code" : null;
  const credentialId = field === "engine" ? claudeCredential.id : codexCredential.id;
  if (backend) {
    fireEvent.change(form.getByLabelText("Engine"), { target: { value: backend } });
  } else {
    fireEvent.change(credential, { target: { value: credentialId } });
  }
  expect(credential).toHaveValue(credentialId);
  expect(create).toBeEnabled();
  fireEvent.click(create);
  const post = fetch.mock.calls.find(([, init]) => init?.method === "POST");
  expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({ backend, credential_id: credentialId });
  await waitFor(() => expect(create).toBeEnabled());
});
