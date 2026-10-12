import { useEffect, useMemo, useState } from "react";
import { IconCheck, IconCpu } from "@tabler/icons-react";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { buildModelOptions } from "../lib/modelOptions";
import { useModelSettings } from "../hooks/useModelSettings";
import { ReasoningEffortSelect } from "./ReasoningEffortSelect";
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
export function ModelPickerDialog({
  session,
  open,
  onOpenChange,
  onApplied,
}: {
  session: SessionInfo;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onApplied?: (session: SessionInfo) => void;
}) {
  const token = useSessionStore((s) => s.token);
  const agents = useSessionStore((s) => s.agents);
  const sessions = useSessionStore((s) => s.sessions);
  const backendModels = useSessionStore((s) => s.backendModels);
  const [typed, setTyped] = useState("");
  const [effort, setEffort] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const agent = agents.find((a) => a.id === session.agent_id) ?? null;
  const { catalog, error: catalogError } = useModelSettings(
    session.backend, session.credential_id || agent?.credential_id, open,
  );
  useEffect(() => {
    if (open) {
      setTyped(session.model ?? "");
      setEffort(session.reasoning_effort ?? "");
      setError(null);
    }
  }, [open, session.id, session.model, session.reasoning_effort]);
  const options = useMemo(
    () =>
      buildModelOptions({
        sessionModel: typed,
        agentModel: agent?.model || catalog?.default_model,
        backendModels: catalog?.models?.map((m) => m.id) ?? backendModels[session.backend],
        inUse: [
          ...agents.map((a) => a.model),
          ...sessions.map((s) => s.model),
        ],
      }),
    [typed, session.backend, agent?.model, catalog, backendModels, agents, sessions]
  );

  const apply = async () => {
    const model = typed.trim() || null;
    const reasoning = effort || null;
    const changes: Record<string, string | null> = {};
    if (model !== (session.model ?? null)) changes.model = model;
    if (reasoning !== (session.reasoning_effort ?? null)) changes.reasoning_effort = reasoning;
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
      useSessionStore
        .getState()
        .setSessions(
          useSessionStore.getState().sessions.map((s) => (s.id === updated.id ? { ...s, ...updated } : s))
        );
      onApplied?.(updated);
      onOpenChange(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "could not change the model");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="model-picker-dialog max-w-md">
        <DialogHeader>
          <DialogTitle>Model and reasoning</DialogTitle>
          <DialogDescription>
            Choose settings for this conversation. Changes apply from the next turn and keep its history.
          </DialogDescription>
        </DialogHeader>

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
          <ReasoningEffortSelect
            id="session-reasoning-effort" value={effort} onChange={setEffort}
            model={typed || agent?.model} catalog={catalog} disabled={busy}
            inherited={`Agent default${agent?.reasoning_effort || catalog?.default_reasoning_effort ? ` (${agent?.reasoning_effort || catalog?.default_reasoning_effort})` : ""}`}
          />
        )}
        {catalogError && <p className="text-xs text-destructive">{catalogError}</p>}
        <Button className="btn-apply-model-settings" disabled={busy} onClick={apply}>Save settings</Button>

        {error && (
          <p className="model-error text-xs text-destructive">{error}</p>
        )}
      </DialogContent>
    </Dialog>
  );
}
