import { accountFetch as fetch } from "../lib/accountFetch";
import { useCallback, useEffect, useRef, useState } from "react";
import { IconArchive, IconPlus, IconTrash, IconUsers } from "@tabler/icons-react";
import { setAgentPinned } from "../api/agents";
import { fetchAgentConnectors, toggleAgentConnector } from "../api/connectors";
import { openAgentChat } from "../lib/openAgentChat";
import { pinnedInOrder } from "../lib/sidebarPins";
import { knownAgentScope, refreshAgents, withAgentRequest } from "../lib/refreshAgents";
import { agentRequestIsCurrent } from "../lib/agentUpdates";
import { useModelSettings } from "../hooks/useModelSettings";
import {
  useSessionStore,
  type Agent,
  type PageTab,
  type SubagentDefinition,
} from "../stores/sessionStore";
import { EmojiPicker } from "./EmojiPicker";
import { ItemLibrary, type LibraryItem } from "./ItemLibrary";
import { PageHeader } from "./PageHeader";
import { ReasoningEffortSelect } from "./ReasoningEffortSelect";
import { ServiceTierSelect } from "./ServiceTierSelect";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Label } from "./ui/label";

const API = `${window.location.origin}/api/agents`;

/** Built-in MCP servers every agent gets unless told otherwise. */
const BUILTIN_MCP = ["ask", "bg", "ask_agent", "research", "schedule"] as const;

type AgentDraft = {
  name: string;
  description: string;
  avatar: string;
  systemPrompt: string;
  credentialId: string;
  model: string;
  reasoningEffort: string;
  serviceTier: string;
  backend: string;
  toolAllow: string;
  toolDeny: string;
  subagents: SubagentDefinition[];
};

function agentFields(draft: AgentDraft) {
  return {
    name: draft.name.trim(),
    description: draft.description.trim(),
    avatar: draft.avatar.trim() || null,
    system_prompt: draft.systemPrompt,
    credential_id: draft.credentialId || null,
    model: draft.model.trim() || null,
    reasoning_effort: draft.reasoningEffort || null,
    service_tier: draft.backend === "codex" ? draft.serviceTier : null,
    backend: draft.backend,
    tool_allow: draft.toolAllow,
    tool_deny: draft.toolDeny,
    // Unnamed rows are drafts the user never filled in, not definitions.
    subagents: draft.subagents.filter((sa) => sa.name.trim()),
  };
}

/** The Agents page — "Agents › All", "Agents › New Agent", "Agents › <name>".
 *
 * Two tabs. **All** lists every agent (sidebar-pins.md §6): the ones pinned
 * to the sidebar, the ones that aren't, and the archived — which is where
 * the old Archived tab went. **Create** (or **Edit**, with an agent open) is
 * the form that replaced the two-pane settings dialog, laid out the way the
 * console design lays it out: identity across the top, the system prompt as
 * the wide centrepiece, then engine / connectors / tool policy in a row of
 * equal-weight panels.
 */
export function AgentFormPage({
  onToggleSidebar,
}: {
  onToggleSidebar: () => void;
}) {
  const account = useSessionStore((s) => s.accountGeneration);
  const opening = useSessionStore((s) => s.agentFormGeneration);
  const agentId = useSessionStore((s) => s.editingAgentId);
  return <AgentFormInstance key={JSON.stringify([account, opening, agentId])} onToggleSidebar={onToggleSidebar} />;
}

function AgentFormInstance({
  onToggleSidebar,
}: {
  onToggleSidebar: () => void;
}) {
  const token = useSessionStore((s) => s.token);
  const agents = useSessionStore((s) => s.agents);
  const archived = useSessionStore((s) => s.archivedAgents);
  const archivedRead = useSessionStore((s) => s.archivedAgentsRead);
  const upsertAgent = useSessionStore((s) => s.upsertAgent);
  const editingAgentId = useSessionStore((s) => s.editingAgentId);
  const credentials = useSessionStore((s) => s.credentials);
  const installations = useSessionStore((s) => s.connectorInstallations);
  const availableBackends = useSessionStore((s) => s.availableBackends);
  const defaultBackend = useSessionStore((s) => s.defaultBackend);
  const backendToolPolicies = useSessionStore((s) => s.backendToolPolicies);
  const showChat = useSessionStore((s) => s.showChat);
  const openAgentForm = useSessionStore((s) => s.openAgentForm);
  const setActiveAgentId = useSessionStore((s) => s.setActiveAgentId);
  const tab = useSessionStore((s) => s.pageTab);
  const setTab = useSessionStore((s) => s.setPageTab);
  const sessions = useSessionStore((s) => s.sessions);

  const editing = agents.find((a) => a.id === editingAgentId)
    ?? archived.find((a) => a.id === editingAgentId) ?? null;
  const [opening] = useState(() => {
    const { accountGeneration, agentFormGeneration, editingAgentId } = useSessionStore.getState();
    return { accountGeneration, agentFormGeneration, editingAgentId };
  });
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const isCurrent = useCallback(() => {
    const current = useSessionStore.getState();
    return mounted.current && current.accountGeneration === opening.accountGeneration
      && current.agentFormGeneration === opening.agentFormGeneration
      && current.editingAgentId === opening.editingAgentId && current.mainView === "agent-form";
  }, [opening]);

  const [initialDraft] = useState<AgentDraft>(() => ({
    name: editing?.name ?? "",
    description: editing?.description ?? "",
    avatar: editing?.avatar ?? "",
    systemPrompt: editing?.system_prompt ?? "",
    backend: editing?.backend ?? defaultBackend,
    credentialId: editing?.credential_id ?? "",
    model: editing?.model ?? "",
    reasoningEffort: editing?.reasoning_effort ?? "",
    serviceTier: editing?.service_tier ?? "default",
    toolAllow: editing?.tool_allow ?? "",
    toolDeny: editing?.tool_deny ?? "",
    subagents: editing?.subagents ?? [],
  }));
  const [name, setName] = useState(initialDraft.name);
  const [description, setDescription] = useState(initialDraft.description);
  const [avatar, setAvatar] = useState(initialDraft.avatar);
  const [systemPrompt, setSystemPrompt] = useState(initialDraft.systemPrompt);
  const [backend, setBackend] = useState(initialDraft.backend);
  const backendChosen = useRef(false);
  const [credentialId, setCredentialId] = useState(initialDraft.credentialId);
  const [model, setModel] = useState(initialDraft.model);
  const [reasoningEffort, setReasoningEffort] = useState(initialDraft.reasoningEffort);
  const [serviceTier, setServiceTier] = useState(initialDraft.serviceTier);
  const { catalog, error: catalogError } = useModelSettings(backend, credentialId, tab === "form");
  const [toolAllow, setToolAllow] = useState(initialDraft.toolAllow);
  const [toolDeny, setToolDeny] = useState(initialDraft.toolDeny);
  const toolPolicy = backendToolPolicies[backend];
  const [enabledConnectors, setEnabledConnectors] = useState<string[]>([]);
  // Pending/successful writes override the opening read; rejection restores
  // the previous override, or the read's value when this was the first edit.
  const [connectorEdits, setConnectorEdits] = useState<Record<string, boolean>>({});
  const pendingConnectors = useRef(new Set<string>());
  const [connectorPending, setConnectorPending] = useState<string[]>([]);
  // Sub-agents this agent brings with it (native-subagents.md §6). Empty is
  // the normal case: the CLI's own built-ins are always available.
  const [subagents, setSubagents] = useState(initialDraft.subagents);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // External updates do not turn untouched fields into draft changes.
  const initialFields = useRef(agentFields(initialDraft));

  const headers = {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
  };

  // The install preference may finish loading after the form opens. Apply it
  // to a new draft, preserving a backend the user has already chosen.
  useEffect(() => {
    if (!editingAgentId && !backendChosen.current) setBackend(defaultBackend);
  }, [defaultBackend, editingAgentId]);

  useEffect(() => {
    if (!editing) {
      setEnabledConnectors([]);
      return;
    }
    fetchAgentConnectors(token, editing.id)
      .then((ids) => {
        if (isCurrent()) setEnabledConnectors(ids);
      })
      .catch(() => {});
    // Other clients' Agent updates do not restart this opening's initial read.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, editingAgentId, isCurrent]);

  useEffect(() => {
    const read = useSessionStore.getState().archivedAgentsRead;
    if (tab === "all" && !read.loading && (!read.loaded || read.invalidated)) {
      void refreshAgents(undefined, "all").catch(() => {});
    }
  }, [tab, token]);

  const save = async () => {
    if (!isCurrent() || !name.trim() || busy) return;
    for (const [field, value, supported] of [
      ["tool_allow", toolAllow, toolPolicy?.allow],
      ["tool_deny", toolDeny, toolPolicy?.deny],
    ] as const) {
      if (value.trim() && !supported) {
        setError(`${backend} cannot enforce ${field}. Clear this policy or select a backend that supports it.`);
        return;
      }
    }
    setBusy(true);
    setError(null);
    const fields = agentFields({
      name, description, avatar, systemPrompt, credentialId, model,
      reasoningEffort, serviceTier, backend, toolAllow, toolDeny, subagents,
    });
    const initial = initialFields.current;
    const backendChanged = initial && fields.backend !== initial.backend;
    // The opening owns create/edit identity; list membership can change while
    // its draft remains mounted, including a confirmed archive or deletion.
    const body = editingAgentId
      ? Object.fromEntries(Object.entries(fields).filter(([key, value]) =>
        JSON.stringify(value) !== JSON.stringify(initial?.[key as keyof typeof fields])
        // Changing the engine intentionally resets its associated settings,
        // including values that another client may have changed in the meantime.
        || (backendChanged && ["credential_id", "model", "reasoning_effort", "service_tier"].includes(key))
      ))
      : { ...fields, mcp_servers: [...BUILTIN_MCP] };
    const request = useSessionStore.getState().beginAgentRequest();
    try {
      const res = await fetch(editingAgentId ? `${API}/${editingAgentId}` : API, {
        method: editingAgentId ? "PATCH" : "POST",
        headers,
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => null);
        throw new Error(detail?.detail || `HTTP ${res.status}`);
      }
      const saved = (await res.json()) as Agent;
      if (!agentRequestIsCurrent(request, useSessionStore.getState())) return;
      upsertAgent(saved, request);
      const current = useSessionStore.getState();
      if (!agentRequestIsCurrent(request, current) || !isCurrent() || current.pageTab !== "form") return;
      if (!current.agents.some((row) => row.id === saved.id)) throw new Error("This agent was archived while saving. Your draft has been kept.");
      // Select what you just made: the sidebar unfolds it, a new session
      // lands under it, and the account menu's "Agent settings" edits it.
      setActiveAgentId(saved.id);
      showChat();
    } catch (e) {
      if (isCurrent() && agentRequestIsCurrent(request, useSessionStore.getState())) setError(e instanceof Error ? e.message : "Failed to save agent");
    } finally {
      useSessionStore.getState().endAgentRequest(request);
      if (isCurrent()) setBusy(false);
    }
  };

  // Archive from either tab: the form's header button archives the agent
  // being edited and leaves the page; the All tab's row button archives that
  // row and stays, so the row visibly moves to the Archived section.
  const archiveAgent = async (agent: Agent): Promise<boolean> => {
    if (agent.is_system) return false;
    if (!window.confirm(`Archive "${agent.name}"? You can restore it later.`))
      return false;
    return withAgentRequest(async (request) => {
      try {
        const res = await fetch(`${API}/${agent.id}/archive`, { method: "POST", headers });
        if (!res.ok) {
          const detail = await res.json().catch(() => null);
          throw new Error(detail?.detail || "Failed to archive agent");
        }
        upsertAgent(await res.json() as Agent, request);
        const current = useSessionStore.getState();
        return agentRequestIsCurrent(request, current) && current.archivedAgents.some((row) => row.id === agent.id);
      } catch (e) {
        if (isCurrent() && agentRequestIsCurrent(request, useSessionStore.getState())) setError(e instanceof Error ? e.message : "Failed to archive agent");
        await refreshAgents(request, knownAgentScope()).catch(() => {});
        return false;
      }
    }, knownAgentScope());
  };

  const archive = async () => {
    if (editing && (await archiveAgent(editing)) && isCurrent()) showChat();
  };

  // Restoring stays on the All tab: the agent reappears under "In sidebar"
  // (the server pins what it restores), which is the confirmation.
  const restore = async (agent: Agent) => {
    setError(null);
    await withAgentRequest(async (request) => {
      try {
        const res = await fetch(`${API}/${agent.id}/unarchive`, { method: "POST", headers });
        if (!res.ok) {
          const detail = await res.json().catch(() => null);
          throw new Error(detail?.detail || "Failed to restore agent");
        }
        upsertAgent(await res.json() as Agent, request);
      } catch (e) {
        if (isCurrent() && agentRequestIsCurrent(request, useSessionStore.getState())) setError(e instanceof Error ? e.message : "Failed to restore agent");
        await refreshAgents(request, knownAgentScope()).catch(() => {});
      }
    }, knownAgentScope());
  };

  const togglePin = async (agent: Agent, pinned: boolean) => {
    setError(null);
    await withAgentRequest(async (request) => {
      try {
        upsertAgent((await setAgentPinned(token, agent.id, pinned)) as Agent, request);
      } catch (e) {
        if (isCurrent() && agentRequestIsCurrent(request, useSessionStore.getState())) setError(e instanceof Error ? e.message : "Failed to update the sidebar");
        await refreshAgents(request, knownAgentScope()).catch(() => {});
      }
    }, knownAgentScope());
  };

  const chat = async (agent: Agent) => {
    let foreground = isCurrent() && useSessionStore.getState().pageTab === "all";
    const current = () => foreground && isCurrent() && useSessionStore.getState().pageTab === "all";
    // Page/tab departures retire this action even if the user returns before
    // its receipt arrives. The clicked Agent is separate from the edit target.
    const unsubscribe = useSessionStore.subscribe(() => {
      if (!current()) { foreground = false; unsubscribe(); }
    });
    setError(null);
    try {
      await openAgentChat(agent.id, current);
    } catch (e) {
      if (current()) setError(e instanceof Error ? e.message : "Failed to open a session");
    } finally {
      unsubscribe();
    }
  };

  const toggleConnector = async (installationId: string, enabled: boolean) => {
    if (!editing || !isCurrent() || pendingConnectors.current.has(installationId)) return;
    pendingConnectors.current.add(installationId);
    setConnectorPending([...pendingConnectors.current]);
    const previous = connectorEdits[installationId];
    setConnectorEdits((current) => ({ ...current, [installationId]: enabled }));
    try {
      await toggleAgentConnector(token, editing.id, installationId, enabled);
    } catch {
      if (isCurrent()) setConnectorEdits((current) => {
        const restored = { ...current };
        if (previous === undefined) delete restored[installationId];
        else restored[installationId] = previous;
        return restored;
      });
    } finally {
      pendingConnectors.current.delete(installationId);
      if (isCurrent()) setConnectorPending([...pendingConnectors.current]);
    }
  };

  const backendCreds = credentials.filter((c) => c.backend === backend);

  return (
    <div className="agent-form-page agent-settings flex min-h-0 flex-1 flex-col">
      <PageHeader
        crumbs={[
          "Agents",
          tab === "all" ? "All" : editingAgentId ? editing?.name ?? initialDraft.name : "New Agent",
        ]}
        onToggleSidebar={onToggleSidebar}
        actions={
          <>
            <TabSwitch
              tab={tab}
              setTab={setTab}
              allCount={agents.length + archived.length}
              allComplete={archivedRead.loaded && !archivedRead.invalidated}
              formLabel={editingAgentId ? "Edit" : "Create"}
            />
            {tab === "form" && (
              <>
                {editing && !editing.archived && !editing.is_system && (
                  <button
                    type="button"
                    className="btn-agent-archive inline-flex items-center gap-1.5 text-[13.5px] text-gray-800 transition-colors hover:text-warn-foreground"
                    onClick={archive}
                  >
                    <IconArchive size={15} />
                    Archive agent
                  </button>
                )}
                <button
                  type="button"
                  className="btn-agent-cancel text-[13.5px] text-gray-800 transition-colors hover:text-gray-950"
                  onClick={showChat}
                >
                  Cancel
                </button>
                <Button
                  className="btn-agent-save"
                  size="sm"
                  onClick={save}
                  disabled={busy || !name.trim()}
                >
                  {editingAgentId ? "Save Agent" : "Create Agent"}
                </Button>
              </>
            )}
          </>
        }
      />

      <div className="page-body">
        <div className="mx-auto w-full max-w-4xl">
          {error && (
            <div className="agent-form-error mb-4 rounded-lg border border-danger-border bg-danger-bg px-3.5 py-2.5 text-[13px] text-destructive">
              {error}
            </div>
          )}

          {tab === "all" ? (
            <>
            {archivedRead.loading && <p role="status">Loading all agents…</p>}
            {archivedRead.error && <p role="alert">Agents not synchronized: {archivedRead.error}
              <button onClick={() => void refreshAgents(undefined, "all").catch(() => {})}>Refresh agents</button>
            </p>}
            <AgentLibrary
              live={agents}
              archived={archived}
              sessionCount={(id) =>
                sessions.filter(
                  (x) => x.agent_id === id && x.origin !== "app"
                ).length
              }
              onTogglePin={togglePin}
              onChat={chat}
              onEdit={(a) => openAgentForm(a.id)}
              onArchive={archiveAgent}
              onRestore={restore}
            />
            </>
          ) : (
            <div className="space-y-6">
              <div className="flex items-start gap-4">
                <div className="mt-7">
                  <EmojiPicker
                    value={avatar}
                    onChange={setAvatar}
                    fallback={(name.trim()[0] || "A").toUpperCase()}
                  />
                </div>
                <div className="grid flex-1 gap-4 md:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="agent-name">Name</Label>
                    <Input
                      id="agent-name"
                      className="agent-name-input w-full"
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      placeholder="sre-ops"
                      disabled={!!editing?.is_system}
                      autoFocus={!editingAgentId}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="agent-role">One-line role</Label>
                    <Input
                      id="agent-role"
                      className="agent-role-input"
                      value={description}
                      onChange={(e) => setDescription(e.target.value)}
                      placeholder="Production stability triage & release checkup"
                    />
                  </div>
                </div>
              </div>

              <div className="space-y-2">
                <Label htmlFor="agent-prompt">
                  System prompt
                  <span className="ml-2 font-mono text-[11px] font-normal text-gray-700">
                    system_prompt
                  </span>
                </Label>
                <textarea
                  id="agent-prompt"
                  className="agent-prompt-input min-h-32 w-full rounded-xl border border-gray-400 bg-card px-4 py-3 text-[13.5px] leading-relaxed text-gray-900 outline-none transition-colors placeholder:text-gray-600 focus:border-primary focus:ring-[3px] focus:ring-primary/10"
                  value={systemPrompt}
                  onChange={(e) => setSystemPrompt(e.target.value)}
                  placeholder="You are a senior SRE. Pull evidence via connectors first, then give an initial read with actionable advice. All write operations request approval first — humans make the final call."
                />
              </div>

              <div className="grid gap-6 md:grid-cols-2">
                <div className="space-y-2">
                  <Label>
                    Engine
                    <span className="ml-2 font-normal text-gray-700">
                      overridable per session
                    </span>
                  </Label>
                  <div className="agent-backend-select grid grid-cols-2 gap-3" role="radiogroup">
                    {["claude-code", "codex"].map((b) => {
                      const picked = backend === b;
                      const usable = availableBackends.includes(b);
                      return (
                        <button
                          key={b}
                          type="button"
                          className={`agent-backend-option btn-agent-backend-${b} rounded-xl border px-4 py-3 text-left transition-colors ${
                            picked
                              ? "border-primary-200 bg-primary-50"
                              : "border-gray-400 hover:bg-gray-50"
                          } ${usable ? "" : "opacity-50"}`}
                          onClick={() => {
                            backendChosen.current = true;
                            if (b !== backend) {
                              setBackend(b);
                              setCredentialId("");
                              setModel("");
                              setReasoningEffort("");
                              setServiceTier("default");
                            }
                          }}
                          role="radio"
                          aria-checked={picked}
                          title={usable ? undefined : `${b} is not installed here`}
                        >
                          <span className="flex items-center gap-2">
                            <span
                              className={`inline-block size-1.5 rounded-full ${
                                usable ? "bg-success" : "bg-gray-500"
                              }`}
                            />
                            <span
                              className={`font-mono text-[12.5px] ${
                                picked ? "font-semibold text-primary" : "text-gray-900"
                              }`}
                            >
                              {b === "claude-code" ? "Claude Code" : "Codex"}
                            </span>
                          </span>
                        </button>
                      );
                    })}
                  </div>
                  <select
                    id="agent-credential"
                    className="agent-credential-select mt-1 h-9 w-full rounded-lg border border-gray-400 bg-card px-3 text-[13.5px] text-gray-900 outline-none transition-colors focus:border-primary focus:ring-[3px] focus:ring-primary/10"
                    value={credentialId}
                    onChange={(e) => setCredentialId(e.target.value)}
                    aria-label="Credential"
                  >
                    <option value="">Host default sign-in</option>
                    {backendCreds.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.label}
                      </option>
                    ))}
                  </select>
                  <Label htmlFor="agent-model">Model</Label>
                  <Input
                    id="agent-model" list="agent-model-options" value={model}
                    onChange={(e) => setModel(e.target.value)} disabled={busy}
                    placeholder={`CLI default${catalog?.default_model ? ` (${catalog.default_model})` : ""}`}
                  />
                  <datalist id="agent-model-options">
                    {catalog?.models?.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
                  </datalist>
                  {backend === "codex" && (
                    <ServiceTierSelect
                      id="agent-service-tier" value={serviceTier} onChange={setServiceTier}
                      model={model} catalog={catalog} disabled={busy}
                    />
                  )}
                  {(backend === "codex" || backend === "claude-code") && (
                    <ReasoningEffortSelect
                      id="agent-reasoning-effort" value={reasoningEffort} onChange={setReasoningEffort}
                      model={model} catalog={catalog} disabled={busy}
                      inherited={`CLI default${catalog?.default_reasoning_effort ? ` (${catalog.default_reasoning_effort})` : ""}`}
                    />
                  )}
                  {catalogError && <p className="text-xs text-destructive">{catalogError}</p>}
                  <p className="font-mono text-[11px] text-gray-700">
                    from Harness · memory lives on the agent, survives engine
                    swaps
                  </p>
                </div>

                <div className="space-y-2">
                  <Label>
                    Connectors
                    {!editingAgentId && (
                      <span className="ml-2 font-normal text-gray-700">
                        enable after creating
                      </span>
                    )}
                  </Label>
                  <div className="agent-connectors card divide-y divide-gray-300">
                    {installations.map((inst) => {
                      const on = connectorEdits[inst.id] ?? enabledConnectors.includes(inst.id);
                      return (
                        <label
                          key={inst.id}
                          className={`flex cursor-pointer items-center gap-3 px-4 py-3 ${
                            editing ? "" : "cursor-not-allowed opacity-60"
                          }`}
                        >
                          <input
                            type="checkbox"
                            className="size-4 accent-[hsl(var(--primary-600))]"
                            checked={on}
                            disabled={!editing || connectorPending.includes(inst.id)}
                            onChange={(e) =>
                              toggleConnector(inst.id, e.target.checked)
                            }
                          />
                          <span className="truncate text-[13.5px] text-gray-900">
                            {inst.label}
                          </span>
                          <span className="ml-auto font-mono text-[11px] text-gray-700">
                            {inst.kind}
                          </span>
                        </label>
                      );
                    })}
                    {installations.length === 0 && (
                      <p className="px-4 py-3 text-[13px] text-gray-700">
                        No connectors installed yet.
                      </p>
                    )}
                  </div>
                  <p className="text-xs text-gray-700">
                    {backend === "codex"
                      ? "These switches select Octopus connectors. Codex can also load native MCP servers from its own configuration."
                      : "These switches select Octopus connectors. Claude uses the MCP configuration supplied for this invocation."}
                  </p>
                </div>
              </div>

              <div className="grid gap-6 md:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="agent-tool-allow">
                    Allowed tools
                    <span className="ml-2 font-mono text-[11px] font-normal text-gray-700">
                      tool_policy
                    </span>
                  </Label>
                  <textarea
                    id="agent-tool-allow"
                    className="agent-tool-allow min-h-20 w-full rounded-xl border border-gray-400 bg-card px-4 py-3 font-mono text-[12.5px] text-gray-900 outline-none transition-colors placeholder:text-gray-600 focus:border-primary focus:ring-[3px] focus:ring-primary/10"
                    value={toolAllow}
                    onChange={(e) => setToolAllow(e.target.value)}
                    disabled={!toolPolicy?.allow && !toolAllow}
                    aria-describedby="agent-tool-allow-help"
                    placeholder="allow (one per line) — empty allows everything"
                  />
                  <p id="agent-tool-allow-help" className="text-xs text-gray-700">
                    {toolPolicy?.allow ? "Only the listed tools are allowed." : "This engine does not support a tool allow list. Clear any saved value before saving or running."}
                  </p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="agent-tool-deny">
                    Denied tools
                  </Label>
                  <textarea
                    id="agent-tool-deny"
                    className="agent-tool-deny min-h-20 w-full rounded-xl border border-gray-400 bg-card px-4 py-3 font-mono text-[12.5px] text-gray-900 outline-none transition-colors placeholder:text-gray-600 focus:border-primary focus:ring-[3px] focus:ring-primary/10"
                    value={toolDeny}
                    onChange={(e) => setToolDeny(e.target.value)}
                    disabled={!toolPolicy?.deny && !toolDeny}
                    aria-describedby="agent-tool-deny-help"
                    placeholder="deny (one per line)"
                  />
                  <p id="agent-tool-deny-help" className="text-xs text-gray-700">
                    {toolPolicy?.deny ? "Uses the engine's native deny rules, including MCP tool patterns." : "This engine does not support tool deny rules. Clear any saved value before saving or running."}
                  </p>
                </div>
              </div>

              <SubagentEditor value={subagents} onChange={setSubagents} backend={backend} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/** Sub-agents an agent brings with it (native-subagents.md §6).
 *
 * These are handed to the CLI as session-scoped definitions, so they *add* to
 * the built-in ones (Explore, Plan, general-purpose) rather than replacing
 * them — which is why an empty list is the normal, fully-functional state and
 * the section says so rather than looking unfinished.
 */
function SubagentEditor({
  value,
  onChange,
  backend,
}: {
  value: SubagentDefinition[];
  onChange: (next: SubagentDefinition[]) => void;
  backend: string;
}) {
  const update = (i: number, patch: Partial<SubagentDefinition>) =>
    onChange(value.map((sa, n) => (n === i ? { ...sa, ...patch } : sa)));

  return (
    <div className="agent-subagents space-y-3">
      <p className="text-xs text-gray-700">
        {backend === "claude-code"
          ? "These definitions are available to Claude Code for this agent."
          : "Definitions are saved, but Codex does not yet use them as native roles or tool restrictions. Switching back to Claude Code keeps them available."}
      </p>
      <div className="flex items-center justify-between">
        <Label>
          Sub-agents
          <span className="ml-2 font-normal text-gray-700">
            extra helpers this agent can hand work to, on top of the built-in
            ones
          </span>
        </Label>
        <button
          type="button"
          className="btn-subagent-add inline-flex items-center gap-1.5 rounded-lg border border-gray-400 px-2.5 py-1 text-[12.5px] text-gray-900 transition-colors hover:border-primary hover:text-primary"
          onClick={() =>
            onChange([...value, { name: "", description: "", prompt: "", model: null, tools: [] }])
          }
        >
          <IconPlus size={14} />
          Add
        </button>
      </div>

      {value.length === 0 ? (
        <p className="subagent-empty text-[12.5px] text-gray-700">
          None — this agent uses the harness's built-in sub-agents.
        </p>
      ) : (
        <div className="space-y-2.5">
          {value.map((sa, i) => (
            <div
              key={i}
              className="subagent-row space-y-2 rounded-xl border border-gray-400 bg-gray-50 p-3"
            >
              <div className="flex items-center gap-2">
                <input
                  className="subagent-name flex-1 rounded-lg border border-gray-400 bg-card px-3 py-1.5 font-mono text-[12.5px] text-gray-900 outline-none focus:border-primary"
                  value={sa.name}
                  onChange={(e) => update(i, { name: e.target.value })}
                  placeholder="name — how the model addresses it (e.g. reviewer)"
                />
                <button
                  type="button"
                  className="btn-subagent-remove inline-flex size-8 items-center justify-center rounded-lg text-gray-600 transition-colors hover:bg-danger-bg hover:text-destructive"
                  onClick={() => onChange(value.filter((_, n) => n !== i))}
                  aria-label={`Remove ${sa.name || "sub-agent"}`}
                >
                  <IconTrash size={14} />
                </button>
              </div>
              <input
                className="subagent-description w-full rounded-lg border border-gray-400 bg-card px-3 py-1.5 text-[12.5px] text-gray-900 outline-none focus:border-primary"
                value={sa.description ?? ""}
                onChange={(e) => update(i, { description: e.target.value })}
                placeholder="when to use it — the model reads this to decide"
              />
              <textarea
                className="subagent-prompt min-h-16 w-full rounded-lg border border-gray-400 bg-card px-3 py-2 text-[12.5px] text-gray-900 outline-none focus:border-primary"
                value={sa.prompt ?? ""}
                onChange={(e) => update(i, { prompt: e.target.value })}
                placeholder="its own system prompt"
              />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function TabSwitch({
  tab,
  setTab,
  allCount,
  allComplete,
  formLabel,
}: {
  tab: PageTab;
  setTab: (t: PageTab) => void;
  allCount: number;
  allComplete: boolean;
  formLabel: string;
}) {
  return (
    <div className="form-tabs flex items-center rounded-lg border border-gray-400 bg-gray-50 p-0.5">
      <button
        type="button"
        className={`btn-tab-all rounded-md px-3 py-1 text-[13px] transition-colors ${
          tab === "all"
            ? "bg-card font-semibold text-gray-950 shadow-sm"
            : "text-gray-800"
        }`}
        onClick={() => setTab("all")}
      >
        All{allCount > 0 ? ` ${allCount}${allComplete ? "" : "+"}` : ""}
      </button>
      <button
        type="button"
        className={`btn-tab-create rounded-md px-3 py-1 text-[13px] transition-colors ${
          tab === "form"
            ? "bg-card font-semibold text-gray-950 shadow-sm"
            : "text-gray-800"
        }`}
        onClick={() => setTab("form")}
      >
        {formLabel}
      </button>
    </div>
  );
}

/** The All tab: every agent, as `ItemLibrary` rows. */
function AgentLibrary({
  live,
  archived,
  sessionCount,
  onTogglePin,
  onChat,
  onEdit,
  onArchive,
  onRestore,
}: {
  live: Agent[];
  archived: Agent[];
  sessionCount: (id: string) => number;
  onTogglePin: (agent: Agent, pinned: boolean) => void;
  onChat: (agent: Agent) => void;
  onEdit: (agent: Agent) => void;
  onArchive: (agent: Agent) => void;
  onRestore: (agent: Agent) => void;
}) {
  const ordered = [
    ...pinnedInOrder(live),
    ...live.filter((a) => a.pinned === false),
    ...archived,
  ];
  const byId = new Map(ordered.map((a) => [a.id, a]));
  const items: LibraryItem[] = ordered.map((a) => {
    const count = a.archived ? 0 : sessionCount(a.id);
    return {
      id: a.id,
      name: a.name,
      description: a.description || "",
      icon: <span className="tile tile-plain">{a.avatar || "🐙"}</span>,
      meta: a.archived
        ? a.backend
        : `${a.backend} · ${count} session${count === 1 ? "" : "s"}`,
      pinned: a.pinned !== false,
      archived: !!a.archived,
      pinLocked: a.is_system,
    };
  });

  return (
    <ItemLibrary
      noun="agents"
      items={items}
      onTogglePin={(item, pinned) => onTogglePin(byId.get(item.id)!, pinned)}
      actions={(item) => {
        const agent = byId.get(item.id)!;
        if (item.archived) {
          return (
            <Button
              size="sm"
              variant="outline"
              className="btn-restore"
              onClick={() => onRestore(agent)}
            >
              Restore
            </Button>
          );
        }
        return (
          <>
            <Button
              size="sm"
              variant="outline"
              className="btn-library-chat"
              onClick={() => onChat(agent)}
            >
              Chat
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="btn-library-edit"
              onClick={() => onEdit(agent)}
            >
              Edit
            </Button>
            {!agent.is_system && (
              <button
                type="button"
                className="btn-library-archive inline-flex size-8 items-center justify-center rounded-lg text-gray-600 transition-colors hover:bg-warn-bg hover:text-warn-foreground"
                onClick={() => onArchive(agent)}
                title="Archive"
                aria-label={`Archive ${agent.name}`}
              >
                <IconArchive size={16} />
              </button>
            )}
          </>
        );
      }}
      empty={
        <>
          <IconUsers size={22} className="mx-auto mb-3 text-gray-600" />
          <p className="text-sm text-gray-900">No agents yet.</p>
        </>
      }
    />
  );
}
