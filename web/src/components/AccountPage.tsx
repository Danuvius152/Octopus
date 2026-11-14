import { accountFetch as fetch } from "../lib/accountFetch";
import { useCallback, useEffect, useState } from "react";
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

/** GET a route, or `null` if it refuses or the server is unreachable.
 *
 * Everything on this page is a read whose failure mode is "show nothing"
 * rather than "throw inside the sidebar" — the auth routes answer 401 to a
 * bearer that has just been replaced, and 403 to the admin sections when you
 * are not one, and neither is worth an error banner.
 */
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
  return <AccountSettings key={token} token={token} onToggleSidebar={onToggleSidebar} />;
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
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const submit = async () => {
    setBusy(true);
    setError(null);
    setDone(false);
    try {
      const res = await fetch(`${API}/password`, {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ current_password: current, new_password: next }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
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
      setError("Could not reach Octopus.");
    } finally {
      setBusy(false);
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
              if (e.key === "Enter") void submit();
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

function Invites({ headers }: { headers: () => HeadersInit }) {
  const [invites, setInvites] = useState<InviteInfo[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Bumped after a create or a revoke; the effect below is the only thing that
  // reads the list, so there is one path onto the screen rather than two.
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let alive = true;
    getJson<InviteInfo[]>(`${API}/invites`, headers()).then((rows) => {
      if (alive && rows) setInvites(rows);
    });
    return () => {
      alive = false;
    };
  }, [headers, reload]);

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API}/invites`, {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ max_uses: 1, ttl_days: 14 }),
      });
      if (!res.ok) {
        setError(`Could not create an invite — HTTP ${res.status}`);
        return;
      }
      setReload((n) => n + 1);
    } catch {
      setError("Could not reach Octopus.");
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (code: string) => {
    await fetch(`${API}/invites/${encodeURIComponent(code)}`, {
      method: "DELETE",
      headers: headers(),
    }).catch(() => null);
    setReload((n) => n + 1);
  };

  const live = invites.filter((i) => !i.revoked_at);

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
          disabled={busy}
          onClick={create}
        >
          <IconPlus size={14} />
          New invite
        </Button>
      }
    >
      {live.length === 0 ? (
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
              </span>
              <button
                type="button"
                className="btn-copy-invite rounded p-1 text-gray-700 hover:bg-gray-100"
                aria-label="Copy invite code"
                onClick={() =>
                  navigator.clipboard?.writeText(invite.code).catch(() => {})
                }
              >
                <IconCopy size={15} />
              </button>
              <button
                type="button"
                className="btn-revoke-invite rounded p-1 text-gray-700 hover:bg-gray-100"
                aria-label="Revoke invite"
                onClick={() => revoke(invite.code)}
              >
                <IconTrash size={15} />
              </button>
            </li>
          ))}
        </ul>
      )}
      {error && <p className="invites-error mt-2 text-xs text-destructive">{error}</p>}
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
  const [people, setPeople] = useState<UserInfo[]>([]);
  const [error, setError] = useState<string | null>(null);

  const [reload, setReload] = useState(0);

  useEffect(() => {
    let alive = true;
    getJson<UserInfo[]>(`${API}/users`, headers()).then((rows) => {
      if (alive && rows) setPeople(rows);
    });
    return () => {
      alive = false;
    };
  }, [headers, reload]);

  const setDisabled = async (id: string, disabled: boolean) => {
    const res = await fetch(`${API}/users/${id}/disabled`, {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ disabled }),
    }).catch(() => null);
    if (res && !res.ok) {
      const data = await res.json().catch(() => null);
      setError(
        typeof data?.detail === "string"
          ? data.detail
          : `Could not update the account — HTTP ${res.status}`
      );
    }
    setReload((n) => n + 1);
  };

  return (
    <Section
      className="account-people"
      title="People"
      description="Disabling an account revokes its tokens and stops it signing in. Nothing it made is deleted."
    >
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
                onClick={() => setDisabled(p.id, !p.disabled_at)}
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
      {error && <p className="people-error mt-2 text-xs text-destructive">{error}</p>}
    </Section>
  );
}
