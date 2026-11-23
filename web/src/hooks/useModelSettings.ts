import { accountFetch as fetch } from "../lib/accountFetch";
import { useEffect, useState } from "react";
import type { ModelSettings } from "../api";
import { useSessionStore } from "../stores/sessionStore";

export type RuntimeModelDefault = { backend: string; credential: string | null; name: string | null };

export function modelOption(catalog: ModelSettings | null, model?: string | null) {
  if (!catalog) return undefined;
  const id = model || catalog.default_model;
  return catalog.models?.find((item) => item.id === id) ?? catalog.saved_model_options?.find((item) => item.id === id);
}

export function useModelSettings(
  backend: string, credentialId?: string | null, enabled = true, savedModels: readonly (string | null | undefined)[] = [],
) {
  const token = useSessionStore((s) => s.token);
  const account = useSessionStore((s) => s.accountGeneration);
  const canRead = enabled && !!token;
  const [read, setRead] = useState(0);
  const query = new URLSearchParams();
  if (credentialId) query.set("credential_id", credentialId);
  for (const model of new Set(savedModels)) if (model) query.append("saved_model", model);
  const url = `/api/backends/${encodeURIComponent(backend)}/models${query.size ? `?${query}` : ""}`;
  const key = JSON.stringify([url, token, account, canRead, read]);
  const [result, setResult] = useState<{ key: string; catalog: ModelSettings | null; error: string | null }>({
    key, catalog: null, error: null,
  });
  // Returning to a previous selection is a new read, even if the in-between
  // request never finished. Retire the previous value before enabling inputs.
  if (result.key !== key) setResult({ key, catalog: null, error: null });
  useEffect(() => {
    if (!canRead) return;
    const controller = new AbortController();
    fetch(url, {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
      cache: "no-store",
    })
      .then(async (res) => {
        if (!res.ok) {
          const detail = await res.json().catch(() => ({}));
          throw new Error(typeof detail.detail === "string"
            ? `${backend}: ${detail.detail}` : `Could not load model settings (HTTP ${res.status}).`);
        }
        const data: ModelSettings = await res.json();
        if (!Array.isArray(data.models)) throw new Error("Could not load model settings.");
        if (!controller.signal.aborted) setResult({ key, catalog: data, error: data.catalog_error ?? null });
      })
      .catch((e: unknown) => {
        if (!controller.signal.aborted) setResult({ key, catalog: null, error: e instanceof Error ? e.message : "Could not load model settings." });
      });
    return () => controller.abort();
  }, [backend, canRead, token, key, url]);
  return {
    ...(canRead && result.key === key ? result : { catalog: null, error: null }),
    loading: canRead && (result.key !== key || (!result.catalog && !result.error)),
    retry: () => setRead((previous) => previous + 1),
  };
}
