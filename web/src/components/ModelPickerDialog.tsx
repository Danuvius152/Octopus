import { useEffect, useEffectEvent, useRef, useState } from "react";
import { accountFetch } from "../lib/accountFetch";
import { loadSessionSnapshots } from "../lib/hydrateSession";
import { selectSession } from "../lib/selectSession";
import { ArchiveDeadline } from "../lib/sessionArchive";
import { sessions as sessionList, type SessionObservation } from "../lib/sessionList";
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
type DraftField = keyof Draft;
type EngineEdits = Partial<Record<BackendKind, Partial<Draft>>>;
const blankDraft = { model: null, credential_id: null, reasoning_effort: null, service_tier: null };
const fieldLabels = {
  model: "Model", credential_id: "Sign-in", reasoning_effort: "Reasoning effort", service_tier: "Speed",
} satisfies Record<DraftField, string>;
const fields = Object.keys(fieldLabels) as DraftField[];

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
  const [initial, setInitial] = useState(session);
  const [backend, setBackend] = useState(initial.backend ?? "claude-code");
  const [edits, setEdits] = useState<EngineEdits>({});
  const [clearChoices, setClearChoices] = useState<{ backend: BackendKind; fields: DraftField[] }>();
  const [engineEdited, setEngineEdited] = useState(false);
  const [conflict, setConflict] = useState<"reload" | "review" | null>(null);
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

  const initialBackend = initial.backend ?? "claude-code";
  const switching = backend !== initialBackend;
  const engineEdits = edits[backend] ?? {};
  const draft: Draft = { ...(switching ? blankDraft : initial), ...engineEdits };
  const unresolved = clearChoices?.backend === backend ? clearChoices.fields : [];
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
  const startNew = switching && (started || initial.has_inputs || session.has_inputs || (session.message_count ?? 0) > 0 ||
    Boolean(session.claude_session_id) || session.status === "running");
  const saveEngine = switching || engineEdited;
  const fieldsToSave = fields.filter((field) => switching || field in engineEdits);
  const hasChanges = fieldsToSave.length > 0 || saveEngine;
  const model = draft.model || defaults?.model;
  const selectedModel = modelOption(catalog, model);
  const efforts = selectedModel?.reasoning_efforts ?? catalog?.reasoning_efforts;
  const tiers = selectedModel?.service_tiers ?? catalog?.service_tiers;
  const defaultModel = defaults?.model || readCatalog?.default_model;
  const defaultModelName = modelOption(readCatalog, defaultModel)?.name ?? defaultModel;
  const modelUnconfirmed = Boolean(draft.model && (switching || draft.model !== initial.model) &&
    !catalog?.models?.some((item) => item.id === draft.model));
  const canApply = hasChanges && !modelUnconfirmed && unresolved.length === 0;
  const setDraft = <K extends keyof Draft>(field: K, value: Draft[K]) => {
    setEdits((current) => ({ ...current, [backend]: { ...current[backend], [field]: value } }));
    if (unresolved.includes(field)) {
      setClearChoices({ backend, fields: unresolved.filter((pending) => pending !== field) });
    }
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

  const refreshConflict = async (observation: SessionObservation, deadline: ArchiveDeadline) => {
    const current = await deadline.wait((async () => {
      const reply = await accountFetch(`/api/sessions/${encodeURIComponent(session.id)}/info`, {
        headers: { Authorization: `Bearer ${token}` }, cache: "no-store", signal: deadline.signal,
      });
      if (!reply.ok) throw new Error(`Could not read current settings (HTTP ${reply.status}).`);
      return await reply.json() as SessionInfo;
    })());
    if (!foreground.current || !sessionList.current(observation)) return;
    if (current?.id !== session.id || current.archived || !current.backend ||
      !Number.isSafeInteger(current.configuration_version) || current.configuration_version < initial.configuration_version) {
      throw new Error("Could not read the current conversation settings.");
    }
    const savedBackend = current.backend;
    const target = engineEdited ? backend : savedBackend;
    if (target !== backend) {
      // Only explicit clears transfer across engines. When an earlier value
      // for the new engine competes with a clear, ask which edit to apply.
      const carried: Partial<Draft> = {};
      const competing: DraftField[] = [];
      for (const field of fields) {
        if (engineEdits[field] !== null) continue;
        if (edits[target]?.[field] != null) competing.push(field);
        else carried[field] = null;
      }
      setEdits((previous) => ({ ...previous, [target]: { ...previous[target], ...carried } }));
      setClearChoices({ backend: target, fields: competing });
    }
    setInitial(current);
    setBackend(target);
    setConflict("review");
  };

  const apply = async () => {
    if (!foreground.current || write.current || unconfirmed || (conflict !== "reload" && !canApply)) return;
    const observation = sessionList.capture();
    const deadline = new ArchiveDeadline();
    write.current = true;
    setBusy(true);
    setError(null);
    let reloading = conflict === "reload";
    let created: SessionInfo | null = null;
    try {
      if (reloading) {
        await refreshConflict(observation, deadline);
        return;
      }
      const updated = await deadline.wait((async () => {
        const changes = Object.fromEntries(fieldsToSave.map((field) => [field, draft[field] ?? null]));
        const url = startNew ? `/api/agents/${session.agent_id}/sessions` : `/api/sessions/${session.id}`;
        const res = await accountFetch(url, {
          method: startNew ? "POST" : "PATCH",
          signal: deadline.signal,
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
          body: JSON.stringify(startNew ? { ...changes, backend, working_dir: session.working_dir }
            : { ...changes, ...(saveEngine ? { backend } : {}), expected_backend: initialBackend,
              expected_configuration_version: initial.configuration_version }),
        });
        if (!res.ok) {
          const unknown = res.status >= 500 || res.status === 408;
          if (startNew && unknown) throw new Error("Creation could not be confirmed.");
          const detail = (await res.json().catch(() => ({}))).detail;
          return { ok: false as const, detail, status: res.status, unknown };
        }
        const row = await res.json() as SessionInfo;
        if (!row?.id || (!startNew && row.id !== session.id) || !Number.isSafeInteger(row.configuration_version)) {
          throw new Error("Invalid conversation response.");
        }
        return { ok: true as const, row };
      })());
      if (!updated) throw new Error("Saving conversation settings could not be confirmed.");
      if (!sessionList.accountCurrent(observation)) return;
      if (!updated.ok) {
        if (!foreground.current || !sessionList.current(observation)) return;
        const { detail } = updated;
        const message = typeof detail === "string" ? detail : detail?.message ?? `Could not save settings (HTTP ${updated.status}).`;
        if (updated.unknown) throw new Error(message);
        if (detail?.code === "conversation_started") setStarted(true);
        if (updated.status === 409 && (detail?.code === "configuration_version_conflict" || detail?.code === "engine_changed")) {
          reloading = true;
          setConflict("reload");
          await refreshConflict(observation, deadline);
          return;
        }
        setError(message);
        return;
      }
      deadline.finish();
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
      sessionList.settingsSaved(row, observation);
      if (!foreground.current || !sessionList.current(observation)) return;
      if (knowsModelDefault) onModelDefault?.({ backend, credential: effectiveCredential || null, name: runtimeDefaultName });
      onApplied?.(row);
      onOpenChange(false);
    } catch (cause) {
      if (!reloading && startNew && sessionList.accountCurrent(observation)) void sessionList.refresh();
      if (!foreground.current || !sessionList.accountCurrent(observation)) return;
      if (reloading) {
        if (sessionList.current(observation)) setError(cause instanceof Error ? cause.message : "Could not read current settings.");
      } else if (startNew) {
        setUnconfirmed(true);
        setError(created ? "The new conversation was saved. Check the session list."
          : "Creation could not be confirmed. Check the session list before creating another conversation.");
      } else if (sessionList.current(observation)) {
        void loadSessionSnapshots(session.id, { fresh: true }).catch(() => {});
        setError(cause instanceof Error ? cause.message : "Could not change conversation settings.");
      }
    } finally {
      deadline.finish();
      write.current = false;
      if (foreground.current) setBusy(false);
    }
  };

  const canEdit = !busy && !unconfirmed && conflict !== "reload";
  const savedDefault = initial.defaults_source === "backend" ? "Engine default" : "Agent default";
  const displayValue = (field: DraftField, value: Draft[DraftField], empty: string) => {
    if (!value) return empty;
    if (field === "service_tier") return value === "fast" ? "Fast" : "Normal";
    if (field === "credential_id") return credentials.find((item) => item.id === value)?.label ?? "Selected sign-in";
    return value;
  };
  const readyFields = fieldsToSave.filter((field) => !unresolved.includes(field));
  const retainedDrafts = Object.entries(edits).flatMap(([engine, values]) => {
    if (engine === backend) return [];
    const pending = fields.filter((field) => (
      field in values && (values[field] !== null || (engineEdits[field] !== null && !unresolved.includes(field)))
    ));
    return pending.length ? [{ engine, values, pending }] : [];
  });
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
            {conflict === "review" && <section className="space-y-1 rounded-lg border border-border p-3 text-sm">
              <p className="font-medium">Current saved settings</p>
              <dl className="grid grid-cols-[max-content_1fr] gap-x-3">
                <dt>Engine</dt><dd>{backendLabel(initialBackend)}</dd>
                <dt>Defaults</dt><dd>{savedDefault}</dd>
                {fields.map((field) => <div className="contents" key={field}>
                  <dt>{fieldLabels[field]}</dt><dd>{displayValue(field, initial[field], savedDefault)}</dd>
                </div>)}
              </dl>
              <p role="status">Review your changes before applying them.</p>
            </section>}
            {conflict === "review" && (saveEngine || readyFields.length > 0) && (
              <section aria-label="Changes to apply" className="space-y-1 rounded-lg border border-border p-3 text-sm">
                <p className="font-medium">Changes to apply</p>
                <dl className="grid grid-cols-[max-content_1fr] gap-x-3">
                  {saveEngine && <><dt>Engine</dt><dd>{backendLabel(backend)}</dd></>}
                  {readyFields.map((field) => <div className="contents" key={field}>
                    <dt>{fieldLabels[field]}</dt><dd>{displayValue(field, draft[field], "Use default")}</dd>
                  </div>)}
                </dl>
              </section>
            )}
            {conflict === "review" && retainedDrafts.map(({ engine, values, pending }) => {
              const label = backendLabel(engine);
              const title = `Unapplied edits for ${label}`;
              const canSwitch = options.some((option) => option === engine) && (engine === initialBackend || !!agent);
              return <section aria-label={title} className="space-y-1 rounded-lg border border-border p-3 text-sm" key={engine}>
                <p className="font-medium">{title}</p>
                <dl className="grid grid-cols-[max-content_1fr] gap-x-3">
                  {pending.map((field) => <div className="contents" key={field}>
                    <dt>{fieldLabels[field]}</dt><dd>{displayValue(field, values[field] ?? null, "Use default")}</dd>
                  </div>)}
                </dl>
                <p>{canSwitch ? `Select ${label} to continue editing this draft.` : `${label} is currently unavailable for switching.`}
                  {" Applying the current changes discards these edits."}</p>
              </section>;
            })}
            {conflict === "review" && unresolved.map((field) => (
              <fieldset className="space-y-2 rounded-lg border border-border p-3 text-sm" key={field}>
                <legend className="font-medium">Choose a value for {fieldLabels[field].toLowerCase()}</legend>
                <div className="flex flex-wrap gap-2">
                  <Button type="button" size="sm" variant="outline" onClick={() => setDraft(field, null)}>
                    Clear {fieldLabels[field].toLowerCase()} override
                  </Button>
                  <Button type="button" size="sm" variant="outline" onClick={() => setDraft(field, draft[field])}>
                    Use earlier {fieldLabels[field].toLowerCase()} choice ({displayValue(field, draft[field], "Use default")})
                  </Button>
                </div>
              </fieldset>
            ))}
            <fieldset className="space-y-2">
              <legend className="text-sm font-medium">Engine</legend>
              <div className="grid grid-cols-2 gap-2">
                {options.map((item) => <label key={item} className="min-w-0">
                  <input type="radio" name="conversation-engine" value={item} checked={backend === item}
                    disabled={item !== initialBackend && !agent} className="peer sr-only"
                    onChange={() => { setBackend(item); setEngineEdited(true); setError(null); }} />
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
          {busy && <p role="status" className="text-xs text-muted-foreground">
            {conflict === "reload" ? "Reading current settings…" : startNew ? "Creating conversation…" : "Saving settings…"}
          </p>}
          <div className="flex shrink-0 justify-end gap-2 border-t border-border pt-3">
            <Button type="button" variant="outline" disabled={busy} onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" className="btn-apply-model-settings"
              disabled={conflict === "reload" ? busy : !canEdit || !canApply}>
              {conflict === "reload" ? "Reload settings" : startNew ? `New session with ${backendLabel(backend)}`
                : conflict === "review" ? "Apply changes" : "Apply"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
