import { accountFetch as fetch } from "../lib/accountFetch";
import { useCallback, useEffect, useRef, useState } from "react";
import { useStore } from "zustand";
import {
  IconCopy,
  IconPlus,
  IconShieldLock,
  IconTrash,
  IconUser,
  IconUserOff,
  IconUserCheck,
} from "@tabler/icons-react";
import type {
  IdentityResponse,
  InviteInfo,
  UserInfo,
  WorkspaceInfo,
} from "../api";
import { useSessionStore } from "../stores/sessionStore";
import { workspaceEditsFor } from "../lib/workspaceEdits";
import { isImeComposing } from "../lib/composerKeys";
import { PageHeader } from "./PageHeader";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Label } from "./ui/label";

const API = `${window.location.origin}/api/auth`;

/** Identity and read-only workspace discovery retain their optional display. */
async function getJson<T>(url: string, headers?: HeadersInit): Promise<T | null> {
  try {
    const res = await fetch(url, headers ? { headers } : undefined);
    return res.ok ? ((await res.json()) as T) : null;
  } catch {
    return null;
  }
}

/** Current account settings, password, and administrator controls. */
export function AccountPage({ onToggleSidebar }: { onToggleSidebar?: () => void }) {
  const token = useSessionStore((s) => s.token);
  const account = useSessionStore((s) => s.accountGeneration);
  return <AccountSettings key={`${account}:${token}`} token={token} onToggleSidebar={onToggleSidebar} />;
}

function AccountSettings({ token, onToggleSidebar }: { token: string; onToggleSidebar?: () => void }) {
  const [identity, setIdentity] = useState<IdentityResponse | null>(null);
  const headers = useCallback(
    () => ({ "Content-Type": "application/json", Authorization: `Bearer ${token}` }),
    [token]
  );

  useEffect(() => {
    let alive = true;
    void getJson<IdentityResponse>(`${API}/identity`, headers()).then((who) => {
      if (alive) setIdentity(who);
    });
    return () => { alive = false; };
  }, [headers]);

  return (
    <div className="account-page flex min-h-0 flex-1 flex-col">
      <PageHeader
        className="account-header"
        icon={<IconUser size={17} className="shrink-0 text-gray-700" />}
        crumbs={["Octopus", "Account"]}
        meta={identity?.is_admin ? "admin" : undefined}
        onToggleSidebar={onToggleSidebar}
      />
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
        <div className="mx-auto flex w-full max-w-2xl flex-col gap-8">
          <IdentityCard identity={identity} />
          {identity?.user_id && <Workspace headers={headers} me={identity} />}
          {identity?.user_id && <ChangePassword headers={headers} />}
          {identity?.is_admin && <Invites headers={headers} />}
          {identity?.is_admin && <People headers={headers} me={identity.user_id ?? null} />}
        </div>
      </div>
    </div>
  );
}

function Section({
  title,
  description,
  actions,
  children,
  className = "",
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={`account-section ${className}`}>
      <div className="mb-3 flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-[15px] font-semibold text-gray-950">{title}</h2>
          {description && (
            <p className="mt-1 text-[12.5px] leading-relaxed text-gray-700">
              {description}
            </p>
          )}
        </div>
        {actions && <div className="shrink-0">{actions}</div>}
      </div>
      {children}
    </section>
  );
}

function IdentityCard({
  identity,
}: {
  identity: IdentityResponse | null;
}) {
  return (
    <Section
      className="account-identity"
      title={identity?.label || "Signed in"}
      description={
        identity?.is_admin
          ? "You administer this install: you can invite people and disable accounts."
          : "Your sessions, agents, applications and files are yours alone."
      }
    >
      {identity?.user_id && (
        <p className="font-mono text-[11px] text-gray-600">{identity.user_id}</p>
      )}
    </Section>
  );
}

function Workspace({
  headers,
  me,
}: {
  headers: () => HeadersInit;
  me: IdentityResponse;
}) {
  return me.is_admin ? <WorkspaceEditor me={me} /> : <WorkspaceReadOnly headers={headers} />;
}

function WorkspaceReadOnly({ headers }: { headers: () => HeadersInit }) {
  const [info, setInfo] = useState<WorkspaceInfo | null>(null);

  useEffect(() => {
    let alive = true;
    getJson<WorkspaceInfo>(`${API}/workspace`, headers()).then((w) => {
      if (alive && w) setInfo(w);
    });
    return () => {
      alive = false;
    };
  }, [headers]);
  return <WorkspaceForm info={info} />;
}

function WorkspaceEditor({ me }: { me: IdentityResponse }) {
  const account = useSessionStore(state => state.accountGeneration);
  const edits = workspaceEditsFor(me.user_id!);
  const { info, adding, phase, error } = useStore(edits.store);
  useEffect(() => edits.attach(account), [edits, account]);
  return <WorkspaceForm info={info} editor={{ adding, busy: phase !== "ready", error,
    change: text => edits.edit(text, account), add: () => edits.add(account), remove: root => edits.remove(root, account),
    recovery: phase === "unknown" ? () => edits.confirm(account) : phase === "load-failed" ? () => edits.retry(account) : undefined,
    recoveryLabel: phase === "unknown" ? "Confirm directory permissions" : "Retry loading" }} />;
}

function WorkspaceForm({ info, editor }: { info: WorkspaceInfo | null; editor?: {
  adding: string; busy: boolean; error: string; change: (text: string) => void; add: () => void; remove: (root: string) => void;
  recovery?: () => void; recoveryLabel: string;
} }) {

  if (!info) return editor?.error ? <div role="alert">{editor.error}<Button onClick={editor.recovery}>{editor.recoveryLabel}</Button></div> : null;
  const roots = info.extra_roots;

  return (
    <Section
      className="account-workspace"
      title="Where you can work"
      description="A session's working directory has to be inside your workspace. Add the directories you work in outside it — a repository you already had, say."
    >
      <p className="workspace-path font-mono text-[11.5px] text-gray-700">
        {info.workspace}
      </p>
      {roots.length > 0 && (
        <ul className="mt-3 divide-y divide-gray-200 rounded-lg border border-gray-200">
          {roots.map((root) => (
            <li key={root} className="root-row flex items-center gap-3 px-3 py-2">
              <code className="root-path min-w-0 flex-1 truncate font-mono text-[12px] text-gray-900">
                {root}
              </code>
              {editor && (
                <button
                  type="button"
                  className="btn-remove-root rounded p-1 text-gray-700 hover:bg-gray-100"
                  aria-label={`Stop allowing ${root}`}
                  disabled={editor.busy}
                  onClick={() => editor.remove(root)}
                >
                  <IconTrash size={15} />
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {editor ? (
        <div className="mt-3 flex max-w-lg items-end gap-2">
          <div className="min-w-0 flex-1 space-y-1.5">
            <Label htmlFor="new-root">Add a directory</Label>
            <Input
              id="new-root"
              className="input-new-root"
              placeholder="/home/you/some-repo"
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
              disabled={editor.busy}
              value={editor.adding}
              onChange={(e) => editor.change(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !isImeComposing(e.nativeEvent)) {
                  e.preventDefault();
                  editor.add();
                }
              }}
            />
          </div>
          <Button
            className="btn-add-root"
            variant="outline"
            disabled={editor.busy || !editor.adding.trim()}
            onClick={editor.add}
          >
            <IconPlus size={14} />
            Add
          </Button>
        </div>
      ) : (
        roots.length === 0 && (
          <p className="mt-2 text-[12.5px] text-gray-700">
            Only your workspace. An admin can open another directory to you.
          </p>
        )
      )}
      {editor?.error && <p role="alert" className="workspace-error mt-2 text-xs text-destructive">{editor.error}</p>}
      {editor?.recovery && <Button variant="outline" onClick={editor.recovery}>{editor.recoveryLabel}</Button>}
    </Section>
  );
}

function ChangePassword({ headers }: { headers: () => HeadersInit }) {
  const operation = useRef<object | null>(null);
  useEffect(() => () => { operation.current = null; }, []);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const submit = async () => {
    if (operation.current) return;
    const original = {};
    operation.current = original;
    const account = useSessionStore.getState().accountGeneration;
    const owns = () => operation.current === original && useSessionStore.getState().accountGeneration === account;
    setBusy(true);
    setError(null);
    setDone(false);
    try {
      const res = await fetch(`${API}/password`, {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ current_password: current, new_password: next }),
      });
      if (!owns()) return;
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        if (!owns()) return;
        setError(
          typeof data?.detail === "string"
            ? data.detail
            : `Could not change the password — HTTP ${res.status}`
        );
        return;
      }
      setCurrent("");
      setNext("");
      setDone(true);
    } catch {
      if (owns()) setError("Could not reach Octopus.");
    } finally {
      if (operation.current === original) {
        operation.current = null;
        if (useSessionStore.getState().accountGeneration === account) setBusy(false);
      }
    }
  };

  return (
    <Section
      className="account-password"
      title="Password"
      description="Changing it signs out every other session. This one keeps working."
    >
      <div className="flex max-w-sm flex-col gap-3">
        <div className="space-y-1.5">
          <Label htmlFor="current-password">Current password</Label>
          <Input
            id="current-password"
            className="input-current-password"
            type="password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="new-password">New password</Label>
          <Input
            id="new-password"
            className="input-new-password"
            type="password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !isImeComposing(e.nativeEvent)) {
                e.preventDefault();
                void submit();
              }
            }}
          />
        </div>
        <Button
          className="btn-change-password self-start"
          disabled={busy}
          onClick={submit}
        >
          Change password
        </Button>
        {error && <p className="password-error text-xs text-destructive">{error}</p>}
        {done && (
          <p className="password-done text-xs text-gray-700">Password changed.</p>
        )}
      </div>
    </Section>
  );
}

async function accountError(response: Response, fallback: string): Promise<string> {
  const body = await response.json().catch(() => null);
  return typeof body?.detail === "string" ? body.detail : `${fallback} — HTTP ${response.status}`;
}

type AccountAction = {
  label: string;
  succeeded?: boolean;
  phase: "writing" | "confirming" | "unknown" | "unconfirmed" | "failed" | "done";
  message: string;
};

function holdsAccountAction(action: AccountAction | undefined) {
  return action !== undefined && action.phase !== "failed" && action.phase !== "done";
}

/** One mounted table owns publication and the actions covered by each read. */
function useAccountList<T>(route: string, headers: () => HeadersInit) {
  const [state, setState] = useState<{ data: T[] | null; loading: boolean; error: string | null }>({
    data: null, loading: true, error: null,
  });
  const lifetime = useRef({ account: useSessionStore.getState().accountGeneration, active: true });
  const reading = useRef(0);
  const [actions, setActions] = useState<Record<string, AccountAction>>({});
  const operations = useRef(new Map<string, {
    owner: typeof lifetime.current; label: string; writing: boolean; checking: boolean; succeeded: boolean;
  }>());
  useEffect(() => {
    const owner = { account: useSessionStore.getState().accountGeneration, active: true };
    lifetime.current = owner;
    const originalOperations = operations.current;
    return () => { owner.active = false; originalOperations.clear(); };
  }, []);
  const read = useCallback(async () => {
    const owner = lifetime.current;
    const owns = () => owner.active && lifetime.current === owner
      && useSessionStore.getState().accountGeneration === owner.account;
    if (!owns()) return false;
    const sequence = ++reading.current;
    const current = () => owns() && reading.current === sequence;
    // A newer read takes over every settled write it starts after. It cannot
    // confirm a write whose reply is still outstanding, or identify a lost one.
    const covered = [...operations.current.entries()].filter(([, operation]) => operation.owner === owner && !operation.writing);
    for (const [, operation] of covered) operation.checking = true;
    setActions(previous => {
      const next = { ...previous };
      for (const [key, operation] of covered) next[key] = { label: operation.label, succeeded: operation.succeeded,
        phase: "confirming", message: "Reading the current list to confirm…" };
      return next;
    });
    setState(previous => ({ ...previous, loading: true, error: null }));
    const settle = (loaded: boolean) => {
      const updates: Record<string, AccountAction> = {};
      for (const [key, operation] of covered) {
        if (operations.current.get(key) !== operation) continue;
        operation.checking = false;
        if (loaded && operation.succeeded) {
          operations.current.delete(key);
          updates[key] = { label: operation.label, succeeded: true, phase: "done", message: "Write confirmed." };
        } else {
          updates[key] = { label: operation.label, succeeded: operation.succeeded,
            phase: loaded ? "unknown" : "unconfirmed",
            message: loaded ? "List refreshed. Write result unknown; this read cannot confirm the original operation."
              : operation.succeeded ? "Write succeeded; confirmation failed." : "Write result unknown; confirmation failed." };
        }
      }
      setActions(previous => ({ ...previous, ...updates }));
    };
    try {
      const response = await fetch(`${API}/${route}`, { headers: headers() });
      if (!response.ok) throw new Error(await accountError(response, `Could not load ${route}`));
      const data = await response.json() as T[];
      if (!current()) return false;
      setState(previous => ({ ...previous, data, error: null }));
      settle(true);
      return true;
    } catch (error) {
      if (current()) {
        setState(previous => ({ ...previous, error: error instanceof Error ? error.message : "Could not reach Octopus." }));
        settle(false);
      }
      return false;
    } finally {
      if (current()) setState(previous => ({ ...previous, loading: false }));
    }
  }, [route, headers]);
  useEffect(() => { void read(); }, [read]);
  const confirm = useCallback(async (key: string) => {
    const original = operations.current.get(key);
    const owns = () => original !== undefined && operations.current.get(key) === original
      && original.owner.active && lifetime.current === original.owner
      && useSessionStore.getState().accountGeneration === original.owner.account;
    if (!original || original.writing || original.checking || !owns()) return;
    await read();
  }, [read]);
  const change = async (key: string, label: string, path: string, init: RequestInit) => {
    const owner = lifetime.current;
    if (!owner.active || useSessionStore.getState().accountGeneration !== owner.account || operations.current.has(key)) return;
    const original = { owner, label, writing: true, checking: false, succeeded: false };
    operations.current.set(key, original);
    const owns = () => operations.current.get(key) === original && owner.active && lifetime.current === owner
      && useSessionStore.getState().accountGeneration === owner.account;
    setActions(previous => ({ ...previous, [key]: { label, phase: "writing", message: "Saving…" } }));
    try {
      const response = await fetch(`${API}/${path}`, { ...init, headers: headers() });
      if (!owns()) return;
      if (!response.ok) {
        const message = await accountError(response, "Could not save the change");
        if (!owns()) return;
        operations.current.delete(key);
        setActions(previous => ({ ...previous, [key]: { label, phase: "failed", message } }));
        return;
      }
      original.writing = false;
      original.succeeded = true;
      await confirm(key);
    } catch {
      if (!owns()) return;
      original.writing = false;
      setActions(previous => ({ ...previous, [key]: { label, phase: "unknown", message: "Could not reach Octopus. Write result unknown." } }));
    }
  };
  return { ...state, read, actions, change, confirm };
}

function ListFeedback({ route, list }: { route: string; list: {
  loading: boolean; error: string | null; read: () => Promise<boolean>;
} }) {
  return <div className="mb-3 space-y-2">
    {list.loading && <p role="status">Loading {route}…</p>}
    {list.error && <p role="alert" className="text-xs text-destructive">{list.error}</p>}
    <Button size="sm" variant="outline" onClick={() => { void list.read(); }}>
      {list.error ? `Retry loading ${route}` : `Refresh ${route}`}
    </Button>
  </div>;
}

function ActionFeedback({ actions, confirm }: {
  actions: Record<string, AccountAction>; confirm: (key: string) => Promise<void>;
}) {
  return Object.entries(actions).map(([key, action]) => <div key={key} className="mt-2 text-xs">
    <p role={action.phase === "failed" || action.phase === "unknown" || action.phase === "unconfirmed" ? "alert" : "status"}>
      {action.label}: {action.message}
    </p>
    {(action.phase === "unknown" || action.phase === "unconfirmed") && <Button size="sm" variant="outline" onClick={() => { void confirm(key); }}>
      {action.phase === "unknown" ? "Check status" : "Retry confirmation"}
    </Button>}
  </div>);
}

function inviteAvailable(invite: InviteInfo, now: number) {
  return !invite.revoked_at && invite.used_count < invite.max_uses
    && (!invite.expires_at || Date.parse(invite.expires_at) > now);
}

function Invites({ headers }: { headers: () => HeadersInit }) {
  const list = useAccountList<InviteInfo>("invites", headers);
  const invites = list.data ?? [];
  const live = invites.filter((i) => !i.revoked_at);
  const [availabilityTime, setAvailabilityTime] = useState(Date.now);
  const now = availabilityTime;
  useEffect(() => {
    const current = Date.now();
    const next = list.data?.reduce((deadline, invite) => inviteAvailable(invite, availabilityTime) && invite.expires_at
      ? Math.min(deadline, Date.parse(invite.expires_at)) : deadline, Infinity) ?? Infinity;
    if (!Number.isFinite(next)) return;
    // One table-owned timer refreshes copy eligibility at its next expiry.
    const timer = window.setTimeout(() => setAvailabilityTime(Date.now()), Math.min(Math.max(0, next - current), 2_147_483_647));
    return () => window.clearTimeout(timer);
  }, [list.data, availabilityTime]);

  return (
    <Section
      className="account-invites"
      title="Invites"
      description="An invite code is one person's way onto this box. It expires in 14 days or when it is used, whichever comes first."
      actions={
        <Button
          size="sm"
          variant="outline"
          className="btn-new-invite"
          disabled={holdsAccountAction(list.actions["new-invite"])}
          onClick={() => { void list.change("new-invite", "New invite", "invites", {
            method: "POST", body: JSON.stringify({ max_uses: 1, ttl_days: 14 }),
          }); }}
        >
          <IconPlus size={14} />
          New invite
        </Button>
      }
    >
      <ListFeedback route="invites" list={list} />
      {list.data !== null && (live.length === 0 ? (
        <p className="invites-empty text-[12.5px] text-gray-700">
          No live invites.
        </p>
      ) : (
        <ul className="divide-y divide-gray-200 rounded-lg border border-gray-200">
          {live.map((invite) => (
            <li
              key={invite.code}
              className="invite-row flex items-center gap-3 px-3 py-2"
            >
              <code className="invite-code min-w-0 flex-1 truncate font-mono text-[12px] text-gray-900">
                {invite.code}
              </code>
              <span className="shrink-0 text-[11.5px] text-gray-600">
                {invite.used_count}/{invite.max_uses} used
                {invite.expires_at
                  ? ` · expires ${invite.expires_at.slice(0, 10)}`
                  : ""}
                {invite.expires_at && Date.parse(invite.expires_at) <= now ? " · expired" : ""}
                {invite.used_count >= invite.max_uses ? " · exhausted" : ""}
              </span>
              {!list.actions[`invite:${invite.code}`]?.succeeded && inviteAvailable(invite, now) && <button
                type="button"
                className="btn-copy-invite rounded p-1 text-gray-700 hover:bg-gray-100"
                aria-label="Copy invite code"
                onClick={() => {
                  if (inviteAvailable(invite, Date.now())) void navigator.clipboard?.writeText(invite.code).catch(() => {});
                }}
              >
                <IconCopy size={15} />
              </button>}
              <button
                type="button"
                className="btn-revoke-invite rounded p-1 text-gray-700 hover:bg-gray-100"
                aria-label="Revoke invite"
                disabled={holdsAccountAction(list.actions[`invite:${invite.code}`])}
                onClick={() => { void list.change(`invite:${invite.code}`, invite.code, `invites/${encodeURIComponent(invite.code)}`, { method: "DELETE" }); }}
              >
                <IconTrash size={15} />
              </button>
            </li>
          ))}
        </ul>
      ))}
      <ActionFeedback actions={list.actions} confirm={list.confirm} />
    </Section>
  );
}

function People({
  headers,
  me,
}: {
  headers: () => HeadersInit;
  me: string | null;
}) {
  const list = useAccountList<UserInfo>("users", headers);
  const people = list.data ?? [];
  return (
    <Section
      className="account-people"
      title="People"
      description="Disabling an account revokes its tokens and stops it signing in. Nothing it made is deleted."
    >
      <ListFeedback route="users" list={list} />
      {list.data !== null && people.length === 0 && <p>No people.</p>}
      <ul className="divide-y divide-gray-200 rounded-lg border border-gray-200">
        {people.map((p) => (
          <li key={p.id} className="person-row flex items-center gap-3 px-3 py-2">
            <span className="min-w-0 flex-1 truncate text-[13px] text-gray-900">
              {p.username}
              {p.is_admin && (
                <IconShieldLock
                  size={13}
                  className="ml-1.5 inline text-gray-600"
                  aria-label="admin"
                />
              )}
            </span>
            <span className="shrink-0 text-[11.5px] text-gray-600">
              {p.disabled_at ? "disabled" : "active"}
            </span>
            {p.id !== me && (
              <button
                type="button"
                className="btn-toggle-disabled rounded p-1 text-gray-700 hover:bg-gray-100"
                aria-label={p.disabled_at ? "Enable account" : "Disable account"}
                disabled={holdsAccountAction(list.actions[`user:${p.id}`])}
                onClick={() => { void list.change(`user:${p.id}`, p.username, `users/${encodeURIComponent(p.id)}/disabled`, {
                  method: "POST", body: JSON.stringify({ disabled: !p.disabled_at }),
                }); }}
              >
                {p.disabled_at ? (
                  <IconUserCheck size={15} />
                ) : (
                  <IconUserOff size={15} />
                )}
              </button>
            )}
          </li>
        ))}
      </ul>
      <ActionFeedback actions={list.actions} confirm={list.confirm} />
    </Section>
  );
}
