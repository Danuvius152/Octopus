import { accountFetch as fetch } from "../lib/accountFetch";
/** Typed fetch wrappers for the /api/connectors routes (connectors.md §5.5).
 *
 * Installations are global; enablement is agent-scoped. The OAuth install is
 * a popup + status-poll flow (the callback lands on the server, not here). */

import type {
  AgentConnectorBindingsResponse,
  AgentConnectorReservation,
  AgentConnectorSettlement,
  ConnectorCatalogEntry,
  ConnectorInstallationInfo,
  ConnectorOAuthClientInfo,
  ConnectorOAuthStartResponse,
  ConnectorOAuthStatusResponse,
  CustomConnectorCreateRequest,
  ReserveAgentConnectorRequest,
  UpdateConnectorRequest,
} from ".";

const API = window.location.origin;

function authHeaders(token: string): HeadersInit {
  return { "Content-Type": "application/json", Authorization: `Bearer ${token}` };
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail =
      body && typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

/** Only the original OAuth operation may observe these receipts after sign-out.
 * All other connector calls retain accountFetch's account admission checks. */
async function oauthJson<T>(path: string, init: RequestInit): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort(init.signal?.reason);
  const timer = setTimeout(() => controller.abort(new DOMException("OAuth response timed out", "TimeoutError")), 30_000);
  init.signal?.addEventListener("abort", abort, { once: true });
  if (init.signal?.aborted) abort();
  let rejectAbort!: () => void;
  const aborted = new Promise<never>((_, reject) => {
    rejectAbort = () => reject(controller.signal.reason);
    controller.signal.addEventListener("abort", rejectAbort, { once: true });
    if (controller.signal.aborted) rejectAbort();
  });
  try {
    return await Promise.race([
      (async () => {
        controller.signal.throwIfAborted();
        return json<T>(await globalThis.fetch(`${API}${path}`, { ...init, signal: controller.signal }));
      })(),
      aborted,
    ]);
  } finally {
    clearTimeout(timer);
    init.signal?.removeEventListener("abort", abort);
    controller.signal.removeEventListener("abort", rejectAbort);
  }
}

export async function fetchCatalog(token: string, signal?: AbortSignal): Promise<ConnectorCatalogEntry[]> {
  return json(
    await fetch(`${API}/api/connectors/catalog`, { headers: authHeaders(token), signal })
  );
}

export async function fetchInstallations(
  token: string, signal?: AbortSignal,
): Promise<ConnectorInstallationInfo[]> {
  return json(await fetch(`${API}/api/connectors`, { headers: authHeaders(token), signal }));
}

export async function startConnectorOAuth(
  token: string,
  kind: string,
  options: { label?: string; installation_id?: string } = {},
): Promise<ConnectorOAuthStartResponse> {
  return oauthJson("/api/connectors/oauth/start", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ kind, label: options.label ?? null, installation_id: options.installation_id }),
  });
}

export async function pollConnectorOAuth(
  token: string,
  loginId: string,
  signal?: AbortSignal,
): Promise<ConnectorOAuthStatusResponse> {
  return oauthJson(`/api/connectors/oauth/status/${loginId}`, {
    headers: authHeaders(token),
    signal,
  });
}

export async function cancelConnectorOAuth(
  token: string,
  loginId: string
): Promise<ConnectorOAuthStatusResponse> {
  return oauthJson("/api/connectors/oauth/cancel", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ login_id: loginId }),
  });
}

export async function updateInstallation(
  token: string,
  id: string,
  body: UpdateConnectorRequest
): Promise<ConnectorInstallationInfo> {
  return json(
    await fetch(`${API}/api/connectors/${id}`, {
      method: "PATCH",
      headers: authHeaders(token),
      body: JSON.stringify(body),
    })
  );
}

export async function deleteInstallation(token: string, id: string): Promise<void> {
  const res = await fetch(`${API}/api/connectors/${id}`, {
    method: "DELETE",
    headers: authHeaders(token),
  });
  if (!res.ok && res.status !== 404) throw new Error(`HTTP ${res.status}`);
}

export async function getOAuthClient(
  token: string,
  kind: string
): Promise<ConnectorOAuthClientInfo> {
  return json(
    await fetch(`${API}/api/connectors/${kind}/oauth-client`, {
      headers: authHeaders(token),
    })
  );
}

export async function setOAuthClient(
  token: string,
  kind: string,
  clientId: string,
  clientSecret: string
): Promise<ConnectorOAuthClientInfo> {
  return json(
    await fetch(`${API}/api/connectors/${kind}/oauth-client`, {
      method: "PUT",
      headers: authHeaders(token),
      body: JSON.stringify({ client_id: clientId, client_secret: clientSecret }),
    })
  );
}

export async function createCustomConnector(
  token: string,
  body: CustomConnectorCreateRequest,
  onCommitted?: () => void,
): Promise<ConnectorCatalogEntry> {
  const response = await fetch(`${API}/api/connectors/custom`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(body),
  });
  // Successful headers prove creation even when acquiring the catalog entry
  // body is still pending. Callers must not publish entry fields from this.
  if (response.ok) onCommitted?.();
  return json(response);
}

export async function deleteCustomConnector(
  token: string,
  kind: string
): Promise<void> {
  const res = await fetch(`${API}/api/connectors/custom/${kind}`, {
    method: "DELETE",
    headers: authHeaders(token),
  });
  if (!res.ok && res.status !== 404) throw new Error(`HTTP ${res.status}`);
}

export async function fetchAgentConnectors(
  token: string,
  agentId: string,
  signal: AbortSignal,
): Promise<AgentConnectorBindingsResponse> {
  const bindings = await json<AgentConnectorBindingsResponse>(
    await fetch(`${API}/api/agents/${encodeURIComponent(agentId)}/connectors`, {
      headers: authHeaders(token),
      cache: "no-store",
      signal: AbortSignal.any([signal, AbortSignal.timeout(30_000)]),
    })
  );
  if (!bindings || !Array.isArray(bindings.installation_ids)) throw new Error("Invalid Connector bindings");
  return bindings;
}

function agentConnectorUrl(agentId: string, installationId: string): string {
  return `${API}/api/agents/${encodeURIComponent(agentId)}/connectors/${encodeURIComponent(installationId)}`;
}

export async function reserveAgentConnector(
  token: string,
  agentId: string,
  installationId: string,
  body: ReserveAgentConnectorRequest,
): Promise<AgentConnectorReservation> {
  const reservation = await json<AgentConnectorReservation>(
    await fetch(`${agentConnectorUrl(agentId, installationId)}/operations`, {
      method: "POST",
      headers: authHeaders(token),
      signal: AbortSignal.timeout(30_000),
      body: JSON.stringify(body),
    }),
  );
  if (!reservation || typeof reservation.operation_id !== "string" || typeof reservation.reserved !== "boolean") {
    throw new Error("Invalid Connector operation reservation");
  }
  return reservation;
}

export async function applyAgentConnector(
  token: string,
  agentId: string,
  installationId: string,
  operationId: string,
): Promise<"applied" | "rejected" | "unknown"> {
  try {
    const response = await fetch(agentConnectorUrl(agentId, installationId), {
      method: "PATCH",
      headers: authHeaders(token),
      signal: AbortSignal.timeout(30_000),
      body: JSON.stringify({ operation_id: operationId }),
    });
    if (response.status === 204) return "applied";
    if ([400, 401, 403, 404, 422].includes(response.status)) return "rejected";
  } catch {
    // A transport failure leaves the original request eligible to reach the server.
  }
  return "unknown";
}

export async function settleAgentConnector(
  token: string,
  agentId: string,
  installationId: string,
  operationId: string,
): Promise<AgentConnectorSettlement> {
  const settlement = await json<AgentConnectorSettlement>(
    await fetch(`${agentConnectorUrl(agentId, installationId)}/operations/${encodeURIComponent(operationId)}/settle`, {
      method: "POST",
      headers: authHeaders(token),
      signal: AbortSignal.timeout(30_000),
    }),
  );
  if (!settlement || typeof settlement.enabled !== "boolean"
    || !["applied", "retired", "unavailable"].includes(settlement.disposition)) {
    throw new Error("Invalid Connector operation settlement");
  }
  return settlement;
}
