import { accountFetch as fetch } from "../lib/accountFetch";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  IconCheck,
  IconCopy,
  IconLogout,
  IconPlus,
  IconX,
} from "@tabler/icons-react";
import type { NotifierInfo } from "../api";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "./ui/tabs";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Label } from "./ui/label";
import { useSessionStore } from "../stores/sessionStore";

interface Props {
  open: boolean;
  onOpenChange: (next: boolean) => void;
  onSignOut: () => void | Promise<void>;
}

const API = `${window.location.origin}/api`;

/** Additive settings dialog — sits *alongside* the three-section sidebar,
 * doesn't replace it. Holds the things that don't naturally fit as
 * sidebar sections: connection info, account, notifier targets. */
export function SettingsDialog({ open, onOpenChange, onSignOut }: Props) {
  const token = useSessionStore((s) => s.token);
  const account = useSessionStore((s) => s.accountGeneration);
  const [copied, setCopied] = useState(false);

  const serverUrl =
    typeof window !== "undefined" ? window.location.origin : "";
  const appVersion = "0.1.0";

  const copyToken = () => {
    navigator.clipboard?.writeText(token).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="settings-dialog max-w-xl">
        <DialogHeader>
          <DialogTitle>Settings</DialogTitle>
          <DialogDescription>
            Connection, account, and notification preferences.
          </DialogDescription>
        </DialogHeader>

        <Tabs defaultValue="general">
          <TabsList className="w-full">
            <TabsTrigger value="general" className="flex-1">
              General
            </TabsTrigger>
            <TabsTrigger value="account" className="flex-1">
              Account
            </TabsTrigger>
            <TabsTrigger value="notifications" className="flex-1">
              Notifications
            </TabsTrigger>
          </TabsList>

          <TabsContent value="general" className="space-y-4">
            <div className="settings-row space-y-1.5">
              <div className="settings-label text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Server
              </div>
              <div className="settings-value text-sm text-foreground font-mono break-all">
                {serverUrl}
              </div>
            </div>
            <div className="settings-row space-y-1.5">
              <div className="settings-label text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Version
              </div>
              <div className="settings-value text-sm text-foreground font-mono">
                {appVersion}
              </div>
            </div>
          </TabsContent>

          <TabsContent value="account" className="space-y-4">
            <div className="settings-row space-y-1.5">
              <div className="settings-label text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Access token
              </div>
              <div className="flex items-center gap-2">
                <div className="flex-1 text-sm text-foreground font-mono break-all rounded-lg border-[0.7px] border-gray-400 px-3 py-2 bg-input">
                  {token || <span className="text-muted-foreground">(none)</span>}
                </div>
                <Button
                  className="btn-copy-token"
                  variant="outline"
                  size="sm"
                  onClick={copyToken}
                  disabled={!token}
                >
                  {copied ? (
                    <>
                      <IconCheck size={16} />
                      Copied
                    </>
                  ) : (
                    <>
                      <IconCopy size={16} />
                      Copy
                    </>
                  )}
                </Button>
              </div>
            </div>

            <div className="settings-row pt-2 border-t border-border">
              <Button
                className="btn-signout"
                variant="outline"
                onClick={() => {
                  onOpenChange(false);
                  void onSignOut();
                }}
              >
                <IconLogout size={16} />
                Sign out
              </Button>
            </div>
          </TabsContent>

          <TabsContent value="notifications">
            <NotifierPanel key={account} token={token} account={account} />
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Notifier panel — list / add / delete webhook targets
// ---------------------------------------------------------------------------

function NotifierPanel({ token, account }: { token: string; account: number }) {
  const [items, setItems] = useState<NotifierInfo[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [label, setLabel] = useState("");
  const [url, setUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  // Refs guard handlers before React commits the pending controls.
  const pendingDeletes = useRef(new Set<string>());
  const [deleting, setDeleting] = useState<ReadonlySet<string>>(new Set());
  const pendingCreate = useRef(false);
  const [creating, setCreating] = useState(false);
  // Writes invalidate earlier reads; accepted snapshots also retire old POST rows.
  const collection = useRef({
    active: false,
    revision: 0,
    observation: {},
    read: null as { revision: number } | null,
    confirm: false,
  });
  const isCurrent = useCallback(() => {
    const state = useSessionStore.getState();
    return collection.current.active && state.accountGeneration === account && state.token === token;
  }, [account, token]);

  // Stable headers keep local edits from restarting the initial read.
  const headers = useMemo(
    () => ({
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    }),
    [token]
  );

  const fetchItems = useCallback(async function readItems() {
    if (!isCurrent()) return;
    const owner = collection.current;
    if (owner.read) {
      owner.confirm = true;
      return;
    }
    const read = { revision: owner.revision };
    owner.read = read;
    try {
      const res = await fetch(`${API}/notifiers`, { headers });
      if (!res.ok) throw new Error(`Server returned ${res.status}.`);
      const items: NotifierInfo[] = await res.json();
      if (!isCurrent() || owner.read !== read) return;
      if (read.revision !== owner.revision) {
        owner.confirm = true;
        return;
      }
      owner.observation = {};
      setItems(items);
    } catch (e) {
      if (!isCurrent() || owner.read !== read) return;
      owner.confirm = false;
      setError((current) => [current, `Unable to load notifier targets: ${String(e)}`].filter(Boolean).join(" "));
    } finally {
      if (owner.read === read) {
        owner.read = null;
        if (owner.confirm && isCurrent()) {
          owner.confirm = false;
          void readItems();
        }
      }
    }
  }, [headers, isCurrent]);

  useEffect(() => {
    const owner = collection.current;
    owner.active = true;
    void fetchItems();
    return () => {
      owner.active = false;
      owner.read = null;
      owner.confirm = false;
    };
  }, [fetchItems]);

  const create = async () => {
    if (!isCurrent() || pendingCreate.current) return;
    setError(null);
    if (!label.trim() || !url.trim()) {
      setError("Both label and URL are required.");
      return;
    }
    pendingCreate.current = true;
    setCreating(true);
    const observation = collection.current.observation;
    try {
      const res = await fetch(`${API}/notifiers`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          type: "webhook",
          label: label.trim(),
          config: { url: url.trim() },
        }),
      });
      if (!res.ok) throw new Error(`Server returned ${res.status}.`);
      const created: NotifierInfo = await res.json();
      if (!isCurrent()) return;
      const owner = collection.current;
      owner.revision++;
      // A read or another write may already have observed and removed this ID.
      if (owner.observation === observation) {
        setItems((current) => [...current.filter((item) => item.id !== created.id), created]);
      } else {
        void fetchItems();
      }
      owner.observation = {};
      setLabel("");
      setUrl("");
      setShowForm(false);
    } catch (e) {
      if (!isCurrent()) return;
      setError(String(e));
      void fetchItems();
    } finally {
      pendingCreate.current = false;
      if (isCurrent()) setCreating(false);
    }
  };

  const remove = async (id: string) => {
    if (!isCurrent() || pendingDeletes.current.has(id)) return;
    pendingDeletes.current.add(id);
    setDeleting(new Set(pendingDeletes.current));
    setError(null);
    try {
      const res = await fetch(`${API}/notifiers/${id}`, {
        method: "DELETE",
        headers,
      });
      if (!isCurrent()) return;
      if (!res.ok) throw new Error(`Server returned ${res.status}.`);
      collection.current.revision++;
      collection.current.observation = {};
      setItems((current) => current.filter((n) => n.id !== id));
    } catch (e) {
      if (!isCurrent()) return;
      setError(String(e));
      // A failed response does not prove the server left the target unchanged.
      void fetchItems();
    } finally {
      pendingDeletes.current.delete(id);
      if (isCurrent()) setDeleting(new Set(pendingDeletes.current));
    }
  };

  return (
    <div className="notifier-panel space-y-3">
      <div className="text-xs text-muted-foreground leading-relaxed">
        Webhook targets receive a POST with{" "}
        <code className="font-mono">
          {"{type, title, message, session_id, session_name}"}
        </code>{" "}
        when a session goes idle.
      </div>

      <div className="notifier-list flex flex-col gap-1">
        {items.length === 0 && !showForm && (
          <div className="text-sm text-muted-foreground italic px-1 py-2">
            No notifier targets configured.
          </div>
        )}
        {items.map((n) => (
          <div
            key={n.id}
            className="notifier-item group flex items-center gap-2 rounded-lg border-[0.7px] border-border px-3 py-2"
          >
            <span className="text-[10px] font-semibold uppercase tracking-wider text-primary-700 bg-primary-100 px-1.5 py-0.5 rounded">
              {n.type}
            </span>
            <span className="flex-1 min-w-0">
              <span className="block text-sm text-foreground truncate">
                {n.label}
              </span>
              <span className="block text-xs text-muted-foreground font-mono truncate">
                {(n.config as { url?: string }).url || ""}
              </span>
            </span>
            <button
              className="btn-delete inline-flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground hover:bg-destructive/10 hover:text-destructive opacity-0 group-hover:opacity-100 transition-opacity"
              onClick={() => remove(n.id)}
              title="Delete notifier"
              disabled={deleting.has(n.id)}
            >
              <IconX size={14} />
            </button>
          </div>
        ))}
      </div>

      {error && (
        <div role="alert" className="text-xs text-destructive">{error}</div>
      )}

      {!showForm && (
        <Button
          variant="outline"
          size="sm"
          className="btn-notifier-add"
          onClick={() => setShowForm(true)}
        >
          <IconPlus size={14} />
          Add webhook
        </Button>
      )}

      {showForm && (
        <div className="notifier-form rounded-lg border-[0.7px] border-border bg-card p-4 space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="notifier-label">Label</Label>
            <Input
              id="notifier-label"
              placeholder="e.g. ntfy.sh / Slack hook"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              disabled={creating}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="notifier-url">Webhook URL</Label>
            <Input
              id="notifier-url"
              placeholder="https://example.com/hook"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              disabled={creating}
            />
          </div>
          <div className="flex justify-end gap-2">
            <Button
              variant="ghost"
              size="sm"
              disabled={creating}
              onClick={() => {
                setShowForm(false);
                setError(null);
              }}
            >
              Cancel
            </Button>
            <Button
              className="btn-notifier-create"
              size="sm"
              onClick={create}
              disabled={creating}
            >
              Save
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
