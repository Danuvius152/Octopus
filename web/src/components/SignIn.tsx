import { accountFetch as fetch } from "../lib/accountFetch";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import type { LoginResponse } from "../api";
import { useSessionStore } from "../stores/sessionStore";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Label } from "./ui/label";

const API = window.location.origin;

/** First setup exchanges a proof for an account; only account bearers sign in. */
type Mode = "loading" | "unavailable" | "setup" | "login" | "register";

export function SignIn({
  /** Called with the bearer to store once the server has issued one. */
  onSignedIn,
}: {
  onSignedIn: (token: string) => void;
}) {
  const [mode, setMode] = useState<Mode>("loading");
  const [setupProof, setSetupProof] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [invite, setInvite] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    let alive = true;
    fetch(`${API}/api/auth/state`)
      .then(async (r) => {
        if (!r.ok) throw new Error("Account state unavailable");
        const body = await r.json();
        if (!body || typeof body.accounts_exist !== "boolean") throw new Error("Invalid account state");
        return body.accounts_exist as boolean;
      })
      .then((d) => {
        if (alive) setMode(d ? "login" : "setup");
      })
      .catch((error: unknown) => {
        if (!alive || error instanceof DOMException && error.name === "AbortError") return;
        setMode("unavailable");
        setError("Could not load sign-in information.");
      });
    return () => {
      alive = false;
    };
  }, [reload]);

  const post = async (path: string, body: object, proof?: string) => {
    const generation = useSessionStore.getState().accountGeneration;
    const current = () => mounted.current && useSessionStore.getState().accountGeneration === generation;
    setBusy(true);
    setError(null);
    try {
      // Setup proof stays outside the store and accountFetch's bearer check.
      const send = proof === undefined ? fetch : globalThis.fetch;
      const res = await send(`${API}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...(proof === undefined ? {} : { Authorization: `Bearer ${proof}` }) },
        body: JSON.stringify(body),
      });
      if (!current()) return;
      const data: (Partial<LoginResponse> & { detail?: unknown }) | null = await res.json().catch(() => null);
      if (!current()) return;
      if (!res.ok) {
        const detail = data?.detail;
        setError(
          typeof detail === "string" && detail.trim()
            ? detail
            : `Could not sign in — HTTP ${res.status}`
        );
        return;
      }
      if (typeof data?.token !== "string" || !data.token.trim()) {
        setError("Could not confirm sign-in.");
        return;
      }
      setSetupProof("");
      onSignedIn(data.token);
    } catch {
      if (current()) setError("Could not reach Octopus.");
    } finally {
      if (current()) setBusy(false);
    }
  };

  const submitLogin = async () => {
    await post("/api/auth/login", { username, password });
  };

  const submitSetup = async () => {
    if (setupProof.trim()) await post("/api/auth/setup", { username, password }, setupProof.trim());
  };

  const submitRegister = async () => {
    await post("/api/auth/register", {
      invite_code: invite,
      username,
      password,
    });
  };

  return (
    <div className="login-screen flex min-h-screen items-center justify-center bg-gray-50 p-6">
      <div className="w-full max-w-sm rounded-2xl border border-gray-300 bg-card p-8 shadow-[0_24px_60px_-28px_rgba(28,44,72,0.28)]">
        <h1 className="mb-6 text-2xl font-bold tracking-tight text-gray-950">
          Octopus
        </h1>

        {mode === "loading" && <p role="status">Loading sign-in information…</p>}
        {mode === "unavailable" && (
          <Button onClick={() => { setError(null); setMode("loading"); setReload((value) => value + 1); }}>Retry</Button>
        )}

        {mode === "setup" && (
          <>
            <p className="mb-6 text-sm leading-relaxed text-gray-800">
              Create the first administrator account for this Octopus.
            </p>
            <div className="space-y-4">
              <SignInField
                id="setup-proof"
                label="Setup token"
                type="password"
                value={setupProof}
                onChange={setSetupProof}
                onEnter={submitSetup}
                placeholder="Paste your setup token"
                autoFocus
              />
              <SignInField id="username" label="Username" value={username} onChange={setUsername} onEnter={submitSetup} />
              <SignInField id="password" label="Password" type="password" value={password} onChange={setPassword} onEnter={submitSetup} />
              <Button className="btn-setup w-full" disabled={busy || !setupProof.trim()} onClick={submitSetup}>
                Set up Octopus
              </Button>
            </div>
          </>
        )}

        {mode === "login" && (
          <>
            <p className="mb-6 text-sm leading-relaxed text-gray-800">
              Sign in to continue.
            </p>
            <div className="space-y-4">
              <SignInField
                id="username"
                label="Username"
                value={username}
                onChange={setUsername}
                onEnter={submitLogin}
                autoFocus
              />
              <SignInField
                id="password"
                label="Password"
                type="password"
                value={password}
                onChange={setPassword}
                onEnter={submitLogin}
              />
              <Button
                className="btn-login w-full"
                disabled={busy}
                onClick={submitLogin}
              >
                Sign in
              </Button>
              <button
                type="button"
                className="btn-have-invite w-full text-center text-xs text-gray-700 hover:underline"
                onClick={() => {
                  setError(null);
                  setMode("register");
                }}
              >
                I have an invite code
              </button>
            </div>
          </>
        )}

        {mode === "register" && (
          <>
            <p className="mb-6 text-sm leading-relaxed text-gray-800">
              Join this Octopus with an invite code.
            </p>
            <div className="space-y-4">
              <SignInField
                id="invite"
                label="Invite code"
                value={invite}
                onChange={setInvite}
                onEnter={submitRegister}
                autoFocus
              />
              <SignInField
                id="username"
                label="Choose a username"
                value={username}
                onChange={setUsername}
                onEnter={submitRegister}
              />
              <SignInField
                id="password"
                label="Choose a password"
                type="password"
                value={password}
                onChange={setPassword}
                onEnter={submitRegister}
              />
              <Button
                className="btn-register w-full"
                disabled={busy}
                onClick={submitRegister}
              >
                Create account
              </Button>
              <button
                type="button"
                className="btn-back-to-sign-in w-full text-center text-xs text-gray-700 hover:underline"
                onClick={() => {
                  setError(null);
                  setMode("login");
                }}
              >
                Back to sign in
              </button>
            </div>
          </>
        )}

        {error && (
          <p className="signin-error mt-4 text-xs text-destructive">{error}</p>
        )}
      </div>
    </div>
  );
}

function SignInField({
  id,
  label,
  value,
  onChange,
  onEnter,
  type = "text",
  placeholder,
  autoFocus = false,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  onEnter: () => void;
  type?: string;
  placeholder?: string;
  autoFocus?: boolean;
}) {
  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type={type}
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        enterKeyHint="go"
        value={value}
        placeholder={placeholder}
        autoFocus={autoFocus}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e: KeyboardEvent) => {
          if (e.key === "Enter") onEnter();
        }}
      />
    </div>
  );
}
