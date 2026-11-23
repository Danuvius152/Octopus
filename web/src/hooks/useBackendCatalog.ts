import { useEffect } from "react";
import type { BackendCatalog } from "../api";
import { accountFetch } from "../lib/accountFetch";
import { useSessionStore, type BackendCatalogRead } from "../stores/sessionStore";

export function backendLabel(backend: string) {
  if (backend === "claude-code") return "Claude Code";
  if (backend === "codex") return "Codex";
  return backend;
}

function isComplete(catalog: BackendCatalog | null): catalog is BackendCatalog {
  return !!catalog && Array.isArray(catalog.supported) && Array.isArray(catalog.available)
    && catalog.supported.includes(catalog.default)
    && catalog.available.every((kind) => catalog.supported.includes(kind))
    && catalog.supported.every((kind) => {
      const models = catalog.models?.[kind];
      const policy = catalog.tool_policy?.[kind];
      return typeof kind === "string" && Array.isArray(models) && models.every((model) => typeof model === "string")
        && typeof policy?.allow === "boolean" && typeof policy?.deny === "boolean";
    });
}

export async function loadBackendCatalog() {
  const { token, accountGeneration, backendCatalog } = useSessionStore.getState();
  if (!token || backendCatalog.status === "loading" || backendCatalog.status === "ready") return;
  const read: BackendCatalogRead = { status: "loading" };
  useSessionStore.setState({ backendCatalog: read });
  const publish = (result: BackendCatalogRead) => useSessionStore.setState((state) => (
    state.token === token && state.accountGeneration === accountGeneration && state.backendCatalog === read
      ? { backendCatalog: result } : state
  ));
  try {
    const response = await accountFetch(`${window.location.origin}/api/backends`, {
      headers: { Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(30_000),
    });
    if (!response.ok) throw new Error(`Engine catalog (HTTP ${response.status})`);
    const catalog: BackendCatalog | null = await response.json();
    if (!isComplete(catalog)) throw new Error("Incomplete engine catalog");
    publish({ status: "ready", catalog });
  } catch {
    publish({ status: "failed" });
  }
}

export function useBackendCatalog() {
  const token = useSessionStore((s) => s.token);
  const accountGeneration = useSessionStore((s) => s.accountGeneration);
  const read = useSessionStore((s) => s.backendCatalog);
  useEffect(() => {
    if (useSessionStore.getState().backendCatalog.status === "idle") void loadBackendCatalog();
  }, [token, accountGeneration]);
  return read;
}
