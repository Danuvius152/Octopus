import { useEffect, useEffectEvent, useRef, useState } from "react";
import { accountFetch } from "../lib/accountFetch";
import { loadSessionSnapshots } from "../lib/hydrateSession";
import { selectSession } from "../lib/selectSession";
import { ArchiveDeadline } from "../lib/sessionArchive";
import { sessions as sessionList } from "../lib/sessionList";
import { backendLabel, useBackendCatalog } from "../hooks/useBackendCatalog";
import { modelOption, useModelSettings, type RuntimeModelDefault } from "../hooks/useModelSettings";
import { useSessionStore, type BackendKind, type SessionInfo } from "../stores/sessionStore";
import { BackendCatalogStatus } from "./BackendCatalogStatus";
import { ModelSelect } from "./ModelSelect";
import { ReasoningEffortSelect } from "./ReasoningEffortSelect";
import { ServiceTierSelect } from "./ServiceTierSelect";
import { Button } from "./ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "./ui/dialog";
import { Label } from "./ui/label";

interface ModelPickerProps {
  session: SessionInfo;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onApplied?: (session: SessionInfo) => void;
  onModelDefault?: (observed: RuntimeModelDefault) => void;
}

type Draft = Pick<SessionInfo, "model" | "credential_id" | "reasoning_effort" | "service_tier">;
const blankDraft = { model: null, credential_id: null, reasoning_effort: null, service_tier: null };
const fields = ["model", "credential_id", "reasoning_effort", "service_tier"] as const;

export function ModelPickerDialog(props: ModelPickerProps) {
  const account = useSessionStore((state) => state.accountGeneration);
  return props.open
    ? <SessionModelPickerDialog key={JSON.stringify([account, props.session.id])} {...props} />
    : null;
}

function SessionModelPickerDialog({ session, open, onOpenChange, onApplied, onModelDefault }: ModelPickerProps) {
  const token = useSessionStore((state) => state.token);
  const agent = useSessionStore((state) => state.agents.find((item) => item.id === session.agent_id));
  const credentials = useSessionStore((state) => state.credentials);
  const engines = useBackendCatalog();
  const [initial] = useState(session);
  const [backend, setBackend] = useState(initial.backend ?? "claude-code");
  const [drafts, setDrafts] = useState<Partial<Record<BackendKind, Draft>>>(() => ({
    [backend]: {
      model: initial.model, reasoning_effort: initial.reasoning_effort,
      service_tier: initial.service_tier, credential_id: initial.credential_id,
    },
  }));
  const [started, setStarted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [unconfirmed, setUnconfirmed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const write = useRef(false);
  const foreground = useRef(false);
  const focusBeforeOpen = useRef(document.activeElement);
  const dismiss = useEffectEvent(() => onOpenChange(false));
  useEffect(() => {
    foreground.current = true;
    const start = useSessionStore.getState();
    const unsubscribe = useSessionStore.subscribe((state) => {
      if (foreground.current && (state.mainView !== start.mainView || state.activeSessionId !== start.activeSessionId)) {
        foreground.current = false;
        dismiss();
      }
    });
    return () => { foreground.current = false; unsubscribe(); };
  }, []);

  const draft = drafts[backend] ?? blankDraft;
  const initialBackend = initial.backend ?? "claude-code";
  const switching = backend !== initialBackend;
  const useAgentDefaults = switching ? agent?.backend === backend : initial.defaults_source !== "backend";
  const defaults = useAgentDefaults ? agent : undefined;
  const defaultsKnown = !useAgentDefaults || !session.agent_id || !!agent;
  const effectiveCredential = draft.credential_id || defaults?.credential_id;
  const { catalog: readCatalog, error: catalogError, loading, retry } = useModelSettings(
    backend, effectiveCredential, defaultsKnown, [switching ? null : initial.model, defaults?.model],
  );
  const catalog = catalogError ? null : readCatalog;
  const allowClear = !!catalogError;
  const knowsModelDefault = !!readCatalog && (!catalogError || !!readCatalog.default_model);
  const runtimeDefaultName = readCatalog?.default_model ? modelOption(readCatalog)?.name ?? readCatalog.default_model : null;
  const savedCredential = session.credential_id || (session.defaults_source !== "backend" ? agent?.credential_id : null);
  const reportModelDefault = useEffectEvent((observed: RuntimeModelDefault) => onModelDefault?.(observed));
  useEffect(() => {
    // A draft for a different engine or login cannot update the saved header.
    if (knowsModelDefault && backend === (session.backend ?? "claude-code") &&
      (effectiveCredential || null) === (savedCredential || null)) {
      reportModelDefault({ backend, credential: effectiveCredential || null, name: runtimeDefaultName });
    }
  }, [backend, effectiveCredential, knowsModelDefault, runtimeDefaultName, savedCredential, session.backend]);
  const usableCredentials = credentials.filter((item) => item.backend === backend);
  const effectiveSignIn = usableCredentials.find((item) => item.id === effectiveCredential);
  const signInProblem = Boolean(effectiveCredential && (effectiveSignIn?.needs_reconnect ||
    (!effectiveSignIn && (credentials.some((item) => item.id === effectiveCredential) || !!catalogError))));
  const [showSignIn, setShowSignIn] = useState(false);
  const startNew = switching && (started || session.has_inputs || (session.message_count ?? 0) > 0 ||
    Boolean(session.claude_session_id) || session.status === "running");
  const changes = Object.fromEntries(fields
    .filter((field) => switching || (draft[field] ?? null) !== (initial[field] ?? null))
    .map((field) => [field, draft[field] ?? null]));
  const model = draft.model || defaults?.model;
  const selectedModel = modelOption(catalog, model);
  const efforts = selectedModel?.reasoning_efforts ?? catalog?.reasoning_efforts;
  const tiers = selectedModel?.service_tiers ?? catalog?.service_tiers;
  const defaultModel = defaults?.model || readCatalog?.default_model;
  const defaultModelName = modelOption(readCatalog, defaultModel)?.name ?? defaultModel;
  const modelUnconfirmed = Boolean(draft.model && (switching || draft.model !== initial.model) &&
    !catalog?.models?.some((item) => item.id === draft.model));
  const setDraft = <K extends keyof Draft>(field: K, value: Draft[K]) => {
    setDrafts((current) => ({ ...current, [backend]: { ...draft, [field]: value } }));
    setError(null);
  };
  const inherited = (value?: string | null) => {
    const source = useAgentDefaults ? "Agent default" : "Engine default";
    return defaultsKnown && value ? `${source} (${value})` : source;
  };
  const inheritedEffort = inherited(defaults?.reasoning_effort || selectedModel?.default_reasoning_effort || readCatalog?.default_reasoning_effort);
  const inheritedSpeed = inherited(defaults?.service_tier === "fast" ? "Fast" : "Normal");
  const incompatibleSpeed = Boolean(defaults?.service_tier && tiers && !tiers.includes(defaults.service_tier));
  const knownEngines = engines.status === "ready" ? engines.catalog.available : [];
  const options = ([...new Set([initialBackend, ...knownEngines])] as BackendKind[]).sort();

  const apply = async () => {
    if (!foreground.current || write.current || unconfirmed || modelUnconfirmed || !Object.keys(changes).length) return;
    const observation = sessionList.capture();
    const deadline = new ArchiveDeadline();
    write.current = true;
    setBusy(true);
    setError(null);
    let created: SessionInfo | null = null;
    try {
      const updated = await deadline.wait((async () => {
        const url = startNew ? `/api/agents/${session.agent_id}/sessions` : `/api/sessions/${session.id}`;
        const res = await accountFetch(url, {
          method: startNew ? "POST" : "PATCH",
          signal: deadline.signal,
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
          body: JSON.stringify(startNew ? { ...changes, backend, working_dir: session.working_dir }
            : { ...changes, ...(switching ? { backend } : {}), expected_backend: initialBackend }),
        });
        if (!res.ok) {
          if (startNew && (res.status >= 500 || res.status === 408)) throw new Error("Creation could not be confirmed.");
          return { error: (await res.json().catch(() => ({}))).detail, status: res.status };
        }
        const row = await res.json() as SessionInfo;
        if (!row?.id) throw new Error("Invalid conversation response.");
        return { row };
      })());
      deadline.finish();
      if (!updated) throw new Error("Saving conversation settings could not be confirmed.");
      if (!sessionList.accountCurrent(observation)) return;
      if ("error" in updated) {
        const detail = updated.error;
        if (foreground.current && sessionList.current(observation)) {
          if (detail?.code === "conversation_started") setStarted(true);
          if (detail?.code === "engine_changed") {
            setUnconfirmed(true);
            void loadSessionSnapshots(session.id, { fresh: true }).catch(() => {});
          }
          setError(typeof detail === "string" ? detail : detail?.message ?? `Could not save settings (HTTP ${updated.status}).`);
        }
        return;
      }
      if (startNew) {
        created = updated.row;
        const confirmation = new ArchiveDeadline();
        let accepted: boolean;
        try {
          accepted = await sessionList.created(created, observation, { deadline: confirmation });
        } finally { confirmation.finish(); }
        if (!foreground.current || !sessionList.accountCurrent(observation)) return;
        if (!accepted || !sessionList.current(observation)) {
          setUnconfirmed(true);
          setError("The new conversation was saved. Open it from the session list.");
          return;
        }
        if (knowsModelDefault) onModelDefault?.({ backend, credential: effectiveCredential || null, name: runtimeDefaultName });
        onOpenChange(false);
        void selectSession(created.id, created.agent_id, { fresh: true });
        return;
      }
      const row = updated.row;
      sessionList.patch(row.id, {
        backend: row.backend, defaults_source: row.defaults_source, has_inputs: row.has_inputs,
        model: row.model, reasoning_effort: row.reasoning_effort, service_tier: row.service_tier,
        credential_id: row.credential_id, can_steer: row.can_steer, can_observe_context: row.can_observe_context,
        can_compact: row.can_compact, compact_unavailable_reason: row.compact_unavailable_reason, can_fork: row.can_fork,
      }, observation);
      if (!foreground.current || !sessionList.current(observation)) return;
      if (knowsModelDefault) onModelDefault?.({ backend, credential: effectiveCredential || null, name: runtimeDefaultName });
      onApplied?.(row);
      onOpenChange(false);
    } catch (cause) {
      if (startNew && sessionList.accountCurrent(observation)) void sessionList.refresh();
      if (!foreground.current || !sessionList.accountCurrent(observation)) return;
      if (startNew) {
        setUnconfirmed(true);
        setError(created ? "The new conversation was saved. Check the session list."
          : "Creation could not be confirmed. Check the session list before creating another conversation.");
      } else {
        if (sessionList.current(observation)) void loadSessionSnapshots(session.id, { fresh: true }).catch(() => {});
        setError(cause instanceof Error ? cause.message : "Could not change conversation settings.");
      }
    } finally {
      deadline.finish();
      write.current = false;
      if (foreground.current) setBusy(false);
    }
  };

  const canEdit = !busy && !unconfirmed;
  return (
    <Dialog open={open} onOpenChange={(next) => {
      if (!next && write.current) return;
      if (!next) foreground.current = false;
      onOpenChange(next);
    }}>
      <DialogContent className="model-picker-dialog flex w-[calc(100%_-_1.5rem)] max-w-md flex-col overflow-hidden"
        onCloseAutoFocus={(event) => {
          if (focusBeforeOpen.current instanceof HTMLElement && focusBeforeOpen.current.isConnected) {
            event.preventDefault();
            focusBeforeOpen.current.focus();
          }
        }}>
        <DialogHeader>
          <DialogTitle>Conversation settings</DialogTitle>
          <DialogDescription>{startNew
            ? "Starts a new conversation. You can still return to this one."
            : switching ? "Changes the engine of this empty conversation." : "Changes apply from the next turn."}
          </DialogDescription>
        </DialogHeader>
        <form className="flex min-h-0 flex-col gap-3" onSubmit={(event) => { event.preventDefault(); void apply(); }}>
          <fieldset disabled={!canEdit} className="min-h-0 space-y-4 overflow-y-auto p-1">
            <fieldset className="space-y-2">
              <legend className="text-sm font-medium">Engine</legend>
              <div className="grid grid-cols-2 gap-2">
                {options.map((item) => <label key={item} className="min-w-0">
                  <input type="radio" name="conversation-engine" value={item} checked={backend === item}
                    disabled={item !== initialBackend && !agent} className="peer sr-only"
                    onChange={() => { setBackend(item); setError(null); }} />
                  <span className="block cursor-pointer rounded-lg border border-border px-3 py-2 text-center text-sm peer-checked:border-primary peer-checked:bg-accent peer-focus-visible:ring-2 peer-focus-visible:ring-ring peer-disabled:opacity-50">
                    {backendLabel(item)}
                  </span>
                </label>)}
              </div>
              <BackendCatalogStatus read={engines} />
            </fieldset>
            <ModelSelect id="session-model" value={draft.model ?? ""} catalog={catalog} allowClear={allowClear}
              inherited={inherited(defaultModelName)} onChange={(value) => setDraft("model", value || null)} />
            {modelUnconfirmed && catalog && <p className="text-xs text-destructive">Choose a model from the current catalog.</p>}
            {(draft.reasoning_effort || defaults?.reasoning_effort || efforts?.length) ? (
              <ReasoningEffortSelect id="session-reasoning-effort" value={draft.reasoning_effort ?? ""}
                onChange={(value) => setDraft("reasoning_effort", value || null)} model={model} catalog={catalog} allowClear={allowClear}
                inherited={defaults?.reasoning_effort && efforts && !efforts.includes(defaults.reasoning_effort)
                  ? `${inheritedEffort} — unavailable for this model` : inheritedEffort} />
            ) : null}
            {(draft.service_tier || defaults?.service_tier === "fast" || incompatibleSpeed || tiers?.includes("fast")) && (
              <ServiceTierSelect id="session-service-tier" value={draft.service_tier ?? ""}
                onChange={(value) => setDraft("service_tier", value === "fast" || value === "default" ? value : null)}
                model={model} catalog={catalog} allowClear={allowClear}
                inherited={incompatibleSpeed ? `${inheritedSpeed} — unavailable for this model` : inheritedSpeed} />
            )}
            {incompatibleSpeed && !draft.service_tier && !tiers?.length &&
              <p className="text-xs text-destructive">In Agent settings, expand Model defaults and set Speed to Normal (default).</p>}
            {!defaultsKnown && <p className="text-xs text-muted-foreground">Agent defaults are unavailable.</p>}
            {loading && <p role="status" className="text-xs text-muted-foreground">Loading model catalog…</p>}
            {catalogError && <div className="flex flex-wrap items-center gap-2 text-xs text-destructive">
              <p role="alert">{catalogError}</p><Button type="button" size="sm" variant="outline" onClick={retry}>Retry catalog</Button>
            </div>}
            <details open={showSignIn || signInProblem} onToggle={(event) => setShowSignIn(event.currentTarget.open)} className="border-t border-border pt-3">
              <summary className="cursor-pointer text-sm font-medium">Sign-in</summary>
              <div className="mt-3 space-y-2">
                <Label htmlFor="session-credential">Sign-in for this conversation</Label>
                <select id="session-credential" value={draft.credential_id ?? ""} className="h-9 w-full rounded-lg border border-border bg-card px-3 text-sm"
                  onChange={(event) => setDraft("credential_id", event.target.value || null)}>
                  <option value="">{useAgentDefaults ? inherited(defaults?.credential_id
                    ? credentials.find((item) => item.id === defaults.credential_id)?.label || "selected credential"
                    : "host sign-in") : "Host default sign-in"}</option>
                  {draft.credential_id && !usableCredentials.some((item) => item.id === draft.credential_id) &&
                    <option value={draft.credential_id}>Selected sign-in (unavailable)</option>}
                  {usableCredentials.map((item) => <option key={item.id} value={item.id}>
                    {item.label} · {item.auth_type === "oauth" ? "OAuth" : "API key"}{item.needs_reconnect ? " · reconnect required" : ""}
                  </option>)}
                </select>
                {signInProblem && <p className="text-xs text-destructive">Choose a compatible sign-in.</p>}
              </div>
            </details>
          </fieldset>
          {error && <p role="alert" className="model-error text-xs text-destructive">{error}</p>}
          {busy && <p role="status" className="text-xs text-muted-foreground">{startNew ? "Creating conversation…" : "Saving settings…"}</p>}
          <div className="flex shrink-0 justify-end gap-2 border-t border-border pt-3">
            <Button type="button" variant="outline" disabled={busy} onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" className="btn-apply-model-settings"
              disabled={!canEdit || modelUnconfirmed || !Object.keys(changes).length}>
              {startNew ? `New session with ${backendLabel(backend)}` : "Apply"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
