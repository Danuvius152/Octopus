import { accountFetch as fetch } from "../lib/accountFetch";
import { useEffect, useState } from "react";
import { IconActivity, IconRefresh } from "@tabler/icons-react";
import { useSessionStore } from "../stores/sessionStore";
import { PageHeader } from "./PageHeader";

const API = window.location.origin;

const WINDOWS = ["1h", "24h", "7d", "30d"] as const;
type Window = (typeof WINDOWS)[number];

type Row = Record<string, string | number | null>;
type Overview = {
  window: string;
  counts: Row[];
  turns: Row[];
  errors: Row[];
  resources: Row[];
  codex?: Row[];
  codex_turns?: Row[];
  codex_runs?: Row[];
  codex_retirement?: Row[];
  codex_native_usage?: Row[];
  codex_steers?: Row[];
  codex_anomalies?: Row[];
};

/** Column labels the raw SQL aliases do not read well as. */
const HEADINGS: Record<string, string> = {
  n: "count",
  ok_n: "ok",
  avg_ms: "avg",
  max_ms: "max",
  p50_ms: "p50",
  p95_ms: "p95",
  p99_ms: "p99",
  last_seen: "last seen",
  error_code: "error",
  installation: "connector",
  weighted_tps: "weighted TPS",
  latest_tps: "last measured TPS",
  latest_at: "last measured at",
  coverage_pct: "timing coverage",
  avg_turn_ttft_ms: "avg turn TTFT",
  timed_responses: "timed responses",
  status: "measurement status",
  preparation_ms: "preparation",
  root_active_ms: "root activity",
  retirement_ms: "retirement",
  start_ack_ms: "start ACK",
  first_output_ms: "first output",
  ack_ms: "steer ACK",
};

function heading(key: string): string {
  return HEADINGS[key] ?? key.replace(/_/g, " ");
}

function cell(key: string, value: string | number | null): string {
  if (value === null || value === undefined) return "—";
  if (key.endsWith("_tps") && typeof value === "number") return `${value.toFixed(2)} tok/s`;
  if (key === "coverage_pct" && typeof value === "number") return `${value.toFixed(1)}%`;
  if ((key === "last_seen" || key === "latest_at") && typeof value === "number") {
    return new Date(value * 1000).toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }
  if (key.endsWith("_ms") && typeof value === "number") {
    return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${Math.round(value)}ms`;
  }
  if (typeof value === "number") {
    return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(2);
  }
  return String(value);
}

/** One report as a table. Empty says so, because a blank panel reads as
 *  "nothing wrong" when it means "nothing recorded". */
function Table({ title, rows }: { title: string; rows: Row[] }) {
  if (!rows?.length) {
    return (
      <section className="mb-6">
        <h3 className="mb-1.5 text-xs font-medium uppercase tracking-wide text-gray-600">
          {title}
        </h3>
        <p className="text-sm text-gray-500">Nothing recorded in this window.</p>
      </section>
    );
  }
  const cols = Object.keys(rows[0]);
  return (
    <section className="mb-6">
      <h3 className="mb-1.5 text-xs font-medium uppercase tracking-wide text-gray-600">
        {title}
      </h3>
      <div className="overflow-x-auto rounded-md border border-gray-200">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-200 bg-gray-50 text-left">
              {cols.map((c) => (
                <th key={c} className="px-3 py-1.5 font-medium text-gray-700">
                  {heading(c)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} className="border-b border-gray-100 last:border-0">
                {cols.map((c) => (
                  <td
                    key={c}
                    className={`px-3 py-1.5 ${
                      typeof r[c] === "number" ? "font-mono tabular-nums" : ""
                    }`}
                  >
                    {cell(c, r[c])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export function MonitorPage({ onToggleSidebar }: { onToggleSidebar?: () => void }) {
  const token = useSessionStore((s) => s.token);
  const [window_, setWindow] = useState<Window>("24h");
  const [snapshot, setSnapshot] = useState<{ token: string; window: Window; data: Overview } | null>(null);
  const data = snapshot?.token === token && snapshot.window === window_ ? snapshot.data : null;
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    let disposed = false;
    let inFlight = false;
    const controller = new AbortController();
    async function load() {
      if (disposed || inFlight || document.visibilityState === "hidden") return;
      inFlight = true;
      setLoading(true);
      try {
        const res = await fetch(`${API}/api/monitor/overview?window=${window_}`, {
          headers: { Authorization: `Bearer ${token}` }, signal: controller.signal,
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          if (!disposed) {
            setError(body.detail ?? `Request failed (${res.status})`);
            if (res.status === 401 || res.status === 403) setSnapshot(null);
          }
          return;
        }
        const next = await res.json() as Overview;
        if (!disposed) {
          setSnapshot({ token, window: window_, data: next });
          setError(null);
        }
      } catch (e) {
        if (!disposed) setError(e instanceof Error ? e.message : String(e));
      } finally {
        inFlight = false;
        if (!disposed) setLoading(false);
      }
    }
    const onVisibility = () => { if (document.visibilityState !== "hidden") void load(); };
    void load();
    const timer = globalThis.setInterval(() => { void load(); }, 5000);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      disposed = true;
      controller.abort();
      globalThis.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [token, window_, refresh]);

  const turnCount = data?.counts.find((c) => c.kind === "turn")?.n ?? 0;

  return (
    <div className="monitor-page flex h-full min-h-0 flex-col">
      <PageHeader
        icon={<IconActivity size={15} />}
        crumbs={["Monitor"]}
        meta={<>{Number(turnCount).toLocaleString()} turns · last {window_}</>}
        actions={
          <div className="flex items-center gap-1.5">
            {WINDOWS.map((w) => (
              <button
                key={w}
                onClick={() => setWindow(w)}
                className={`rounded px-2 py-0.5 text-xs ${
                  w === window_
                    ? "bg-gray-200 font-medium text-gray-900"
                    : "text-gray-600 hover:bg-gray-100"
                }`}
              >
                {w}
              </button>
            ))}
            <button
              onClick={() => setRefresh((value) => value + 1)}
              aria-label="Refresh"
              className="rounded p-1 text-gray-600 hover:bg-gray-100"
            >
              <IconRefresh size={14} />
            </button>
          </div>
        }
        onToggleSidebar={onToggleSidebar}
      />

      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        {error && (
          <p className="mb-4 rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-sm text-gray-700">
            {error}
          </p>
        )}
        {loading && !data && <p className="text-sm text-gray-500">Loading…</p>}
        {data && (
          <>
            <Table title="Codex effective output TPS" rows={(data.codex ?? []).map((row) => ({
              ...row,
              status: Number(row.timed_responses) === 0 ? "Missing timing"
                : Number(row.timed_responses) < Number(row.responses) ? "Partial timing" : "Measured",
            }))} />
            <p className="mb-4 text-xs text-gray-500">
              Includes reasoning. Output tokens ÷ client response time, including first-token wait;
              excludes tool execution, background waits and user pauses. Last completed measurements,
              held during tools and waits; refreshes every 5s while visible. Coverage is timed responses
              ÷ observed usage records. TTFT is per turn. Different tasks and settings affect comparisons.
            </p>
            <details className="mb-6">
              <summary className="cursor-pointer text-sm text-gray-600">Recent Codex turns</summary>
              <Table title="Recent logical requests (up to 20, within window)" rows={data.codex_turns ?? []} />
            </details>
            <Table title="Native execution phases" rows={(data.codex_runs ?? []).map((row) => ({
              request_id: row.request_id, root_turns: row.root_turns, child_turns: row.child_turns,
              input_state: row.input_state, resource_state: row.resource_state,
              preparation_mode: row.preparation_mode, spawn_ms: row.spawn_ms,
              preparation_ms: row.preparation_ms, start_ack_ms: row.start_ack_ms,
              first_output_ms: row.first_output_ms, root_active_ms: row.root_active_ms,
              retirement_ms: row.retirement_ms,
              usage_coverage: Number(row.metrics_unavailable) > 0 || Number(row.metrics_capacity_drops) > 0
                || row.metrics_collection_complete === 0
                ? "Incomplete" : "Source verified",
            }))} />
            <p className="mb-4 text-xs text-gray-500">
              A product request can have multiple attempts; each accepted attempt binds one root turn,
              with separate child turns and model responses. Retirement measures resource cleanup,
              including cancel, drain and exit; it is outside model generation time.
            </p>
            <details className="mb-6">
              <summary className="cursor-pointer text-sm text-gray-600">Native counters and input timing</summary>
              <Table title="Provider cumulative snapshots" rows={data.codex_native_usage ?? []} />
              <p className="mb-4 text-xs text-gray-500">
                Snapshots are not added to response usage. They may include restored history and
                do not count model requests. Missing usage or timing remains unknown.
              </p>
              <Table title="Steer acceptance timing" rows={data.codex_steers ?? []} />
              <p className="mb-4 text-xs text-gray-500">An acceptance ACK does not prove the model consumed the input.</p>
              <Table title="Owner budget clocks" rows={(data.codex_runs ?? []).map((row) => ({
                request_id: row.request_id, overall_elapsed_ms: row.overall_elapsed_ms,
                overall_limit_ms: row.overall_limit_ms, idle_elapsed_ms: row.idle_elapsed_ms,
                idle_limit_ms: row.idle_limit_ms, idle_suspended: row.idle_suspended,
              }))} />
              <Table title="Retirement grace phases" rows={data.codex_retirement ?? []} />
              <p className="mb-4 text-xs text-gray-500">Only phases entered by the Harness appear, with their actual timeout and elapsed time.</p>
            </details>
            {!!data.codex_anomalies?.length && <Table title="Unaccepted native execution usage" rows={data.codex_anomalies} />}
            <Table title="activity" rows={data.counts} />
            <Table title="turns" rows={data.turns} />
            <Table title="top errors" rows={data.errors} />
            <Table title="resources" rows={data.resources} />
            <p className="text-xs text-gray-500">
              Anything not shown here is a query away —{" "}
              <code className="font-mono">octopus monitor --since {window_}</code>, or
              sqlite3 against octopus-metrics.db. Kept 30 days.
            </p>
          </>
        )}
      </div>
    </div>
  );
}
