import { accountFetch as fetch } from "../lib/accountFetch";
import { sessions as sessionList } from "../lib/sessionList";
import { useEffect, useMemo, useRef, useState } from "react";
import { IconCheck, IconCpu } from "@tabler/icons-react";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { buildModelOptions } from "../lib/modelOptions";
import { useModelSettings } from "../hooks/useModelSettings";
import { ReasoningEffortSelect } from "./ReasoningEffortSelect";
import { ServiceTierSelect } from "./ServiceTierSelect";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";
import { Input } from "./ui/input";
import { Button } from "./ui/button";
import { Label } from "./ui/label";

const API_URL = window.location.origin;

/**
 * `/model` — which model runs *this* conversation.
 *
 * Per session, not per agent: wanting a stronger model for one hard question is
 * not wanting to re-point every session the agent owns. Clearing the override
 * puts the session back to following its agent, so the agent stays the place
 * where the durable answer lives.
 *
 * The free-text field is not a fallback, it is the escape hatch that makes the
 * list safe to keep short: both CLIs accept names this build cannot know, and a
 * model released next week must not need an Octopus release to be usable.
 */
interface ModelPickerProps {
  session: SessionInfo;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onApplied?: (session: SessionInfo) => void;
}

export function ModelPickerDialog(props: ModelPickerProps) {
  return <SessionModelPickerDialog key={props.session.id} {...props} />;
}

function SessionModelPickerDialog({ session, open, onOpenChange, onApplied }: ModelPickerProps) {
  const token = useSessionStore((s) => s.token);
  const agents = useSessionStore((s) => s.agents);
  const credentials = useSessionStore((s) => s.credentials);
  const sessions = useSessionStore((s) => s.sessions);
  const backendModels = useSessionStore((s) => s.backendModels);
  const [typed, setTyped] = useState("");
  const [effort, setEffort] = useState("");
  const [serviceTier, setServiceTier] = useState("");
  const [credentialChoice, setCredentialChoice] = useState(session.credential_id ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const agent = agents.find((a) => a.id === session.agent_id) ?? null;
  const agentKnown = !session.agent_id || agent !== null;
  const { catalog, error: catalogError } = useModelSettings(
    session.backend, credentialChoice || agent?.credential_id, open && agentKnown,
  );
  const currentCatalog = credentialChoice === (session.credential_id ?? "") ? catalog : null;
  const usableCredentials = credentials.filter((c) => c.backend === session.backend);
  const credentialId = session.credential_id || agent?.credential_id;
  const credential = credentials.find((c) => c.id === credentialId);
  const effective = (
    override: string | null | undefined, inherited: string | null | undefined, fallback: string, fallbackSource = "CLI default",
  ) => override ? { value: override, source: "Session override" }
    : !agentKnown ? { value: "Unknown", source: "Agent details unavailable" }
    : inherited ? { value: inherited, source: "Agent default" } : { value: fallback, source: fallbackSource };
  const speed = effective(session.service_tier, agent?.service_tier, "default", "Normal default");
  const signIn = effective(session.credential_id, agent?.credential_id, "CLI default sign-in");
  const currentSettings = [
    { label: "Model", ...effective(session.model, agent?.model, currentCatalog?.default_model || "CLI default") },
    { label: "Reasoning effort", ...effective(session.reasoning_effort, agent?.reasoning_effort, currentCatalog?.default_reasoning_effort || "CLI default") },
    ...(session.backend === "codex" || session.service_tier || agent?.service_tier ? [{
      label: "Speed", ...speed, value: speed.value === "default" ? "Normal" : speed.value === "fast" ? "Fast" : speed.value,
    }] : []),
    { label: "Credential", ...signIn, value: credential?.label || (credentialId ? "Selected credential" : signIn.value) },
  ];
  useEffect(() => {
    if (open) {
      setTyped(session.model ?? "");
      setEffort(session.reasoning_effort ?? "");
      setServiceTier(session.service_tier ?? "");
      setCredentialChoice(session.credential_id ?? "");
      setError(null);
    }
  }, [open, session.id, session.model, session.reasoning_effort, session.service_tier, session.credential_id]);
  const options = useMemo(
    () =>
      buildModelOptions({
        sessionModel: typed,
        agentModel: agentKnown ? agent?.model || catalog?.default_model : undefined,
        backendModels: catalog?.models?.map((m) => m.id) ?? backendModels[session.backend],
        inUse: [
          ...agents.map((a) => a.model),
          ...sessions.map((s) => s.model),
        ],
      }),
    [typed, session.backend, agent?.model, agentKnown, catalog, backendModels, agents, sessions]
  );

  const apply = async () => {
    const model = typed.trim() || null;
    const reasoning = effort || null;
    const changes: Record<string, string | null> = {};
    if (model !== (session.model ?? null)) changes.model = model;
    if (reasoning !== (session.reasoning_effort ?? null)) changes.reasoning_effort = reasoning;
    if ((serviceTier || null) !== (session.service_tier ?? null)) changes.service_tier = serviceTier || null;
    if ((credentialChoice || null) !== (session.credential_id ?? null)) changes.credential_id = credentialChoice || null;
    const observation = sessionList.capture();
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/sessions/${session.id}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify(changes),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(typeof detail.detail === "string" ? detail.detail : `HTTP ${res.status}`);
      }
      const updated: SessionInfo = await res.json();
      // Keep the list in step so the composer hint and this dialog agree
      // without a refetch.
      sessionList.patch(updated.id, {
        model: updated.model, reasoning_effort: updated.reasoning_effort,
        service_tier: updated.service_tier, credential_id: updated.credential_id,
      }, observation);
      if (!mounted.current || !sessionList.current(observation)) return;
      onApplied?.(updated);
      onOpenChange(false);
    } catch (e) {
      if (mounted.current && sessionList.current(observation)) setError(e instanceof Error ? e.message : "could not change the model");
    } finally {
      if (mounted.current) setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="model-picker-dialog max-w-md">
        <DialogHeader>
          <DialogTitle>Model, reasoning and speed</DialogTitle>
          <DialogDescription>
            Choose settings for this conversation. Changes apply from the next turn and keep its history.
          </DialogDescription>
        </DialogHeader>

        <p className="text-xs text-muted-foreground">
          {`This conversation uses ${session.backend}.${agent?.backend && agent.backend !== session.backend
            ? ` Agent default backend: ${agent.backend}. The conversation keeps its backend and history.` : ""}`}
        </p>
        <table aria-label="Current effective settings" className="w-full text-left text-xs">
          <tbody>
            {currentSettings.map((setting) => (
              <tr key={setting.label}>
                <th scope="row" className="py-1 pr-3 font-medium">{setting.label}</th>
                <td className="break-all py-1">{setting.value}</td>
                <td className="py-1 pl-3 text-muted-foreground">{setting.source}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!agentKnown && <p className="text-xs text-muted-foreground">Agent details are unavailable; inherited settings cannot be determined.</p>}
        <p className="text-xs text-muted-foreground">
          Each field inherits independently. Clear an override to use its Agent default.
          Correct a credential and model controls together here when needed; the header credential picker changes only the sign-in.
          Tool policies are configured in Agent settings.
        </p>

        <div className="space-y-2">
          <Label htmlFor="session-credential">Credential</Label>
          <select
            id="session-credential" value={credentialChoice} onChange={(e) => setCredentialChoice(e.target.value)}
            disabled={busy} className="h-9 w-full rounded-lg border border-border bg-card px-3 text-sm"
          >
            <option value="">Agent default{!agentKnown ? " (details unavailable)"
              : agent?.credential_id ? ` (${credentials.find((c) => c.id === agent.credential_id)?.label || "selected credential"})`
              : " (CLI default sign-in)"}</option>
            {credentialChoice && !usableCredentials.some((c) => c.id === credentialChoice) && (
              <option value={credentialChoice}>Selected credential (not listed for {session.backend})</option>
            )}
            {usableCredentials.map((c) => <option key={c.id} value={c.id}>{c.label}{c.needs_reconnect ? " (reconnect required)" : ""}</option>)}
          </select>
        </div>

        <div className="model-options flex max-h-56 flex-col gap-1 overflow-y-auto">
          {options.map((o) => (
            <button
              key={o.value ?? "__agent__"}
              type="button"
              disabled={busy}
              aria-pressed={o.current}
              className="btn-model-option group flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-2 text-left text-sm hover:bg-accent disabled:opacity-50"
              onClick={() => setTyped(o.value ?? "")}
            >
              <IconCpu size={14} className="shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1 truncate">{o.label}</span>
              {o.source === "used" && o.value !== null && (
                <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
                  in use
                </span>
              )}
              {o.current && (
                <IconCheck size={14} className="shrink-0 text-primary" />
              )}
            </button>
          ))}
        </div>

        <Label htmlFor="session-model">Model name</Label>
        <form
          className="model-freeform flex items-center gap-2 pt-1"
          onSubmit={(e) => {
            e.preventDefault();
            void apply();
          }}
        >
          <Input
            id="session-model"
            value={typed}
            disabled={busy}
            onChange={(e) => setTyped(e.target.value)}
            placeholder="…or type any model name"
            className="h-9 text-sm"
          />
        </form>

        {session.backend === "codex" && (
          <ServiceTierSelect
            id="session-service-tier" value={serviceTier} onChange={setServiceTier}
            model={typed || agent?.model} catalog={catalog} disabled={busy}
            inherited={`Agent default (${!agentKnown ? "details unavailable" : agent?.service_tier === "fast" ? "Fast" : "Normal"})`}
          />
        )}
        {(session.backend === "codex" || session.backend === "claude-code") && (
          <ReasoningEffortSelect
            id="session-reasoning-effort" value={effort} onChange={setEffort}
            model={typed || agent?.model} catalog={catalog} disabled={busy}
            inherited={`Agent default${!agentKnown ? " (details unavailable)"
              : agent?.reasoning_effort || catalog?.default_reasoning_effort ? ` (${agent?.reasoning_effort || catalog?.default_reasoning_effort})` : ""}`}
          />
        )}
        {catalogError && <p className="text-xs text-destructive">{catalogError}</p>}
        <Button className="btn-apply-model-settings" disabled={busy} onClick={apply}>Save settings</Button>
        {agent && (
          <Button variant="outline" onClick={() => {
            onOpenChange(false);
            useSessionStore.getState().openAgentForm(agent.id);
          }}>Agent settings</Button>
        )}

        {error && (
          <p className="model-error text-xs text-destructive">{error}</p>
        )}
      </DialogContent>
    </Dialog>
  );
}
