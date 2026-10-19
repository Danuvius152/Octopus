import { accountFetch as fetch } from "../lib/accountFetch";
import { useEffect, useState } from "react";
import type { ModelSettings } from "../api";
import { useSessionStore } from "../stores/sessionStore";

export function useModelSettings(backend: string, credentialId?: string | null, enabled = true) {
  const token = useSessionStore((s) => s.token);
  const key = JSON.stringify([backend, credentialId || null, token]);
  const [result, setResult] = useState<{ key: string; catalog: ModelSettings | null; error: string | null } | null>(null);
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    const query = credentialId ? `?credential_id=${encodeURIComponent(credentialId)}` : "";
    fetch(`/api/backends/${encodeURIComponent(backend)}/models${query}`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
      .then(async (res) => {
        if (!res.ok) throw new Error(`Could not load model settings (HTTP ${res.status}).`);
        const data: ModelSettings = await res.json();
        if (!Array.isArray(data.models)) throw new Error("Could not load model settings.");
        if (!controller.signal.aborted) setResult({ key, catalog: data, error: null });
      })
      .catch((e: unknown) => {
        if (!controller.signal.aborted) setResult({ key, catalog: null, error: e instanceof Error ? e.message : "Could not load model settings." });
      });
    return () => controller.abort();
  }, [backend, credentialId, enabled, token, key]);
  return enabled && result?.key === key ? result : { catalog: null, error: null };
}
